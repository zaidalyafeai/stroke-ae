import os
import torch, numpy as np
dist = torch.distributions
import matplotlib.pyplot as plt
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence, pad_packed_sequence

from quickdraw.quickdraw import QuickDraw
from beziercurve import draw_bezier

def drawsketch(ctrlpts, ratws, st_starts, n_stroke, draw_axis=plt.gca(), invert_y=True, plot_markers = True):
    ctrlpts, ratws, st_starts = ctrlpts[:n_stroke], ratws[:n_stroke], st_starts[:n_stroke]
    # ctrlpts = ctrlpts.view(-1, ctrlpts.shape[-1] // 2, 2)
    
    z_ = torch.ones((ratws.shape[0], 1), device=ratws.device) * 5. # sigmoid(5.) is close to 1
    ratws = torch.cat([z_, ratws, z_], 1)
    for ctrlpt, ratw, st_start in zip(ctrlpts, torch.sigmoid(ratws), st_starts):

        if len(ctrlpt.shape) == 1:
            ctrlpt = ctrlpt.view(-1, 2)
        
        # Decode the DelP1..DelPn
        P0 = torch.zeros(1, 2, device=ctrlpts[0].device)
        # breakpoint()
        ctrlpt = torch.cat([P0, ctrlpt], 0)
        ctrlpt = torch.cumsum(ctrlpt, 0)

        ctrlpt = ctrlpt.detach().cpu().numpy()
        ratw = ratw.detach().cpu().numpy()
        st_start = st_start.detach().cpu().numpy()
        # over-writing this for now

        draw_bezier(ctrlpt, rWeights=None, start_xy=st_start, draw_axis=draw_axis, annotate=False,
            ctrlPointPlotKwargs=dict(color='g', linestyle='--', marker='X', alpha=0.4),
            curvePlotKwagrs=dict(color='r'), plot_markers = plot_markers)
    if invert_y:
        draw_axis.invert_yaxis()

def stroke_embed(batch, initials, embedder, bezier_degree, bezier_degree_low, variational=False, inf_loss=False):
    h_initial, c_initial = initials
    # Redundant, but thats fine
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    # accumulate all info into these empty lists
    sketches_ctrlpt, sketches_ratw, sketches_st_starts, sketches_stopbits = [], [], [], []
    deg_losses = []
    n_strokes = []
    
    for sk, _ in batch:
        # for each sketch in the batch
        st_starts = torch.tensor([st[0,:2] for st in sk], device=device)
        sk = [torch.tensor(st[:,:-1], device=device) - st_start for st, st_start in zip(sk, st_starts)]
        ls = [st.shape[0] for st in sk]
        sk = pad_sequence(sk, batch_first=True)
        sk = pack_padded_sequence(sk, ls, batch_first=True, enforce_sorted=False)

        if embedder.rational:
            emb_ctrlpt, emb_ratw = embedder(sk, h_initial, c_initial)
        else:
            if not inf_loss:
                emb_ctrlpt = embedder(sk, h_initial, c_initial, inf_loss=False)
            else:
                emb_ctrlpt, deg_loss = embedder(sk, h_initial, c_initial, inf_loss=True)
                # breakpoint()
        
        if not inf_loss:
            emb_ctrlpt = emb_ctrlpt[bezier_degree - bezier_degree_low]
            sketches_ctrlpt.append(emb_ctrlpt.view(len(ls), -1))
        else:
            sketches_ctrlpt.append(emb_ctrlpt)
            deg_losses.append(deg_loss)
        # breakpoint()

        if embedder.rational:
            sketches_ratw.append(emb_ratw)
        sketches_st_starts.append(st_starts)
        # create stopbits
        stopbit = torch.zeros(len(ls), 1, device=device); stopbit[-1, 0] = 1.
        sketches_stopbits.append(stopbit)
        n_strokes.append(len(ls))
    
    n_strokes = torch.tensor(n_strokes, device=device)
    if not inf_loss:
        sketches_ctrlpt = pad_sequence(sketches_ctrlpt, batch_first=True)
    
    if embedder.rational:
        sketches_ratw = pad_sequence(sketches_ratw, batch_first=True)
    sketches_st_starts = pad_sequence(sketches_st_starts, batch_first=True)
    sketches_stopbits = pad_sequence(sketches_stopbits, batch_first=True, padding_value=1.0)

    # For every sketch in a batch:
    #   For every stroke in the sketch:
    #     1. (Control Point, Rational Weights) pair
    #     2. Start location of the stroke with respect to a global reference (of the sketch)
    if embedder.rational:
        return sketches_ctrlpt, sketches_ratw, sketches_st_starts, sketches_stopbits, n_strokes
    else:
        if not inf_loss:
            return sketches_ctrlpt, sketches_st_starts, sketches_stopbits, n_strokes
        else:
            return (sketches_ctrlpt, deg_losses), sketches_st_starts, sketches_stopbits, n_strokes

def inference(qdl, model, embedder, emblayers, embhidden, layers, hidden, n_mix,
    nsamples, rsamples, variational, bezier_degree, bezier_degree_low, savefile, device, invert_y):
    with torch.no_grad():
        fig, ax = plt.subplots(nsamples, (rsamples + 1), figsize=(rsamples * 8, nsamples * 4))
        for i, B in enumerate(qdl):

            h_initial_emb = torch.zeros(emblayers * 2, 256, embhidden, dtype=torch.float32)
            c_initial_emb = torch.zeros(emblayers * 2, 256, embhidden, dtype=torch.float32)
            h_initial = torch.zeros(layers * 2, 1, hidden, dtype=torch.float32)
            c_initial = torch.zeros(layers * 2, 1, hidden, dtype=torch.float32)
            if torch.cuda.is_available():
                h_initial, h_initial_emb, c_initial, c_initial_emb = h_initial.cuda(), h_initial_emb.cuda(), c_initial.cuda(), c_initial_emb.cuda()

            with torch.no_grad():
                if model.rational:
                    ctrlpts, ratws, starts, _, n_strokes = stroke_embed(B, (h_initial_emb, c_initial_emb), embedder, bezier_degree, bezier_degree_low)
                else:
                    ctrlpts, starts, _, n_strokes = stroke_embed(B, (h_initial_emb, c_initial_emb), embedder, bezier_degree, bezier_degree_low)
                    ratws = torch.ones(ctrlpts.shape[0], ctrlpts.shape[1], model.n_ratw, device=ctrlpts.device)

                _cpad = torch.zeros(ctrlpts.shape[0], 1, ctrlpts.shape[2], device=device)
                _rpad = torch.zeros(ratws.shape[0], 1, ratws.shape[2], device=device)
                _spad = torch.zeros(starts.shape[0], 1, starts.shape[2], device=device)
                ctrlpts = torch.cat([_cpad, ctrlpts], dim=1)
                ratws = torch.cat([_rpad, ratws], dim=1)
                starts = torch.cat([_spad, starts], dim=1)

            for i in range(256):
                if i == nsamples:
                    break
                
                n_stroke = n_strokes[i]
                drawsketch(ctrlpts[i,1:n_stroke+1,:], ratws[i,1:n_stroke+1,:], starts[i,1:n_stroke+1,:],
                    n_stroke, ax[i, 0], invert_y=invert_y)

                for r in range(rsamples):
                    if model.rational:
                        out_param_mu, out_param_std, out_param_mix, _ = model((h_initial, c_initial), 
                            ctrlpts[i,:n_stroke,:].unsqueeze(0), ratws[i,:n_stroke,:].unsqueeze(0), starts[i,:n_stroke,:].unsqueeze(0))
                        n_stroke = out_ctrlpts.shape[0]
                        drawsketch(out_ctrlpts, out_ratws, out_starts, n_stroke, ax[i, 1+r], invert_y=invert_y)
                    else:
                        if model.variational:
                            out_ctrlpts, out_starts = model((h_initial, c_initial), ctrlpts[i,:n_stroke,:].unsqueeze(0), None, starts[i,:n_stroke,:].unsqueeze(0), inference=True)
                        else:
                             out_ctrlpts, out_starts= model((h_initial, c_initial), ctrlpts[i,:n_stroke,:].unsqueeze(0), None, starts[i,:n_stroke,:].unsqueeze(0), inference=True)
                        out_ratws = torch.ones(out_ctrlpts.shape[1], model.n_ratw) # FAKE IT
                        
                        n_stroke = out_ctrlpts.shape[0]
                        drawsketch(out_ctrlpts, out_ratws, out_starts, n_stroke, ax[i, 1+r], invert_y=invert_y)

            break # just one batch enough

        plt.xticks([]); plt.yticks([])
        plt.savefig(savefile)
        plt.close()
