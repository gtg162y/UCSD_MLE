"""
Author:
    Amit Patel (amitpatel.gt@gmail.com)
Description:
    Main code to train the text summarizer
"""

import pandas as pd
import torch
from rouge import Rouge
import utils
import queue
from itertools import count

logger = utils.create_logger('evaluate.log')

def evaluate_model(model, dataloader, abs_idx2word, device, print_example=False):
    rouge_scores = []
    num_samples = 0
    max_num_samples = 16
    model.eval()
    with torch.no_grad():
        for x,yt,_,yorig,_ in dataloader:
            num_samples += x.shape[0]
            x, yt = x.to(device), yt.to(device)
            y = yt[:,:-1]
            # y = model(x, y) #teacher forcing
            # y = torch.argmax(y, axis=1).to(dtype=torch.int32) #teacher forcing
            y = model.evaluate(x)
            seq_len = y.shape[1]+1
            ygt = yt[:,1:seq_len]
            rouge_scores.append(evaluate_helper(y, ygt, yorig, abs_idx2word, print_example))
            if num_samples > max_num_samples: break
    model.train()
    rouge_1 = sum([r[0] for r in rouge_scores])/len(rouge_scores)
    rouge_2 = sum([r[1] for r in rouge_scores])/len(rouge_scores)
    rouge_l = sum([r[2] for r in rouge_scores])/len(rouge_scores)
    if print_example:
        logger.debug(f'Example\nPrediction\n{rouge_scores[0][3]}\nTarget\n{rouge_scores[0][4]}')
        logger.debug(f'For this example, Rouge-1 is {rouge_scores[0][0]:.4f}, Rouge-2 is {rouge_scores[0][1]:.4f}, and Rouge-l is {rouge_scores[0][2]:.4f}')
    return rouge_1, rouge_2, rouge_l


def evaluate_helper(y, ygt, yorig, abs_idx2word, print_example):
    '''
    y is an int32 tensor of shape BxVxL (where V is the size of abstract vocabulary)
    ygt is an int32 tensor of shape BxL
    '''
    y = y.cpu().numpy()
    ygt = ygt.cpu().numpy()
    assert y.shape[0] == ygt.shape[0], f'y {y.shape} and ygt {ygt.shape} have different batch sizes'
    batch_size = y.shape[0]
    rouge_1, rouge_2, rouge_l = [], [], []
    for b in range(batch_size):
        pred = pd.Series(y[b,:]).map(abs_idx2word).tolist()
        target = pd.Series(ygt[b,:]).map(abs_idx2word).tolist()
        pred = ' '.join(pred)
        target = ' '.join(target)
        # orig_abs = yorig[b]
        # target = orig_abs
        rouge_evaluator = Rouge()
        rouge_score = rouge_evaluator.get_scores(pred, target) #pred is the first argument (https://pypi.org/project/rouge/)
        rouge_1.append(rouge_score[0]['rouge-1']['f'])
        rouge_2.append(rouge_score[0]['rouge-2']['f'])
        rouge_l.append(rouge_score[0]['rouge-l']['f'])
    rouge_1 = sum(rouge_1)/len(rouge_1)
    rouge_2 = sum(rouge_2)/len(rouge_2)
    rouge_l = sum(rouge_l)/len(rouge_l)
    if print_example: res = (rouge_1, rouge_2, rouge_l, pred, target)
    else: res = (rouge_1, rouge_2, rouge_l)
    return res


def eval_single_batch(h0, c0, decoder, predMaxLen, yEnc=None, encMask=None):
    """ Greedy function for next word prediciton """
    device=h0.device
    start_token = 3*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    stop_token = 4*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    pad_token = 0*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    hprev, cprev = h0, c0
    yprev = start_token
    predictions = [yprev]
    while yprev != stop_token and yprev != pad_token and len(predictions) < predMaxLen:
        # yprev: 1x1, ynxt: 1xVx1, hprev/hnxt: Nx1xH, cprev/cnxt: Nx1xH
        if yEnc is not None:
            ynxt, (hnxt, cnxt) = decoder(yprev, (hprev, cprev), yEnc, encMask)
        else:
            ynxt, (hnxt, cnxt) = decoder(yprev, (hprev, cprev))
        hprev, cprev = hnxt, cnxt
        yprev = torch.argmax(ynxt, dim=1).to(dtype=torch.int32)
        predictions.append(yprev)
    return torch.cat(predictions, dim=1) #1xL


def eval_single_batch_beamsearch(h0, c0, decoder, predMaxLen, yEnc=None, encMask=None, beamSize=3, maxNumHyp=10):
    """ Next word prediction using beam search: this method is inbetween the greedy and the optimal approach.
        The priority score is based upon the probability of a sentence, and since longer senteces will have lower probability 
        than shorter ones, we normalize the priority score by dividing by tIter (i.e. sentence length).

        We are actually using negative log probability as we are using a min type priority queue.
    """
    device=h0.device
    start_token = 3*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    stop_token = 4*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    pad_token = 0*torch.ones(size=(1,1), dtype=torch.int32, device=device)
    hprev, cprev = h0, c0
    yprev = start_token
    yprevs = []
    negLogProb = 0.0
    pqueue = queue.PriorityQueue(maxsize=beamSize) #this is a min priority queue
    completedHyp_pqueue = queue.PriorityQueue(maxsize=maxNumHyp+beamSize)
    '''
    Need tie breaker b'cse of the way pqueue works is say the item is (a,b,c), so when comparing two items 
    if a is equal, then it will compare b, if b is equal then compare c and so forth. 
    So to address the issue when it gets to comparing two tensors with one than one elements, 
    we add a counter to make sure if the first element is same between two items the second one will be unique 
    i.e. tie breaker so that it does not go to comparing the tensors. #https://stackoverflow.com/questions/39504333/

    In this case we are using negLogProb/tIter as the first level priority followed by a unique number from counter as tie breaker
    '''
    pQTieBreaker = count()
    for _ in range(beamSize):
        yprevs_ = yprevs.copy()
        yprevs_.append(yprev)
        prediction = (negLogProb/1.0, next(pQTieBreaker), negLogProb, (yprevs_, hprev, cprev))
        pqueue.put(prediction)
    tIter = 1
    while tIter < predMaxLen:
        temp_pqueue = queue.PriorityQueue(maxsize=beamSize**2)
        for _ in range(beamSize):
            _, _, negLogProb, (yprevs, hprev, cprev) = pqueue.get()
            yprev = yprevs[-1]
            if yprev == stop_token or yprev == pad_token: 
                prediction = negLogProb/tIter, next(pQTieBreaker), negLogProb, (yprevs, hprev, cprev)
                completedHyp_pqueue.put(prediction) #add it to the completed hypotheses' queue
                continue
            # yprev: 1x1, ynxt: 1xVx1, hprev/hnxt: Nx1xH, cprev/cnxt: Nx1xH
            if yEnc is not None:
                ynxt, (hnxt, cnxt) = decoder(yprev, (hprev, cprev), yEnc, encMask)
            else:
                ynxt, (hnxt, cnxt) = decoder(yprev, (hprev, cprev))
            hprev, cprev = hnxt, cnxt
            ynxt =  torch.nn.functional.log_softmax(ynxt, dim=1) #1xVx1
            ynxt = torch.topk(ynxt, beamSize, dim=1)
            tempYprev = ynxt.indices.to(dtype=torch.int32) #1xbeamSizex1
            tempPscore = -1*ynxt.values #multiply by -1 as we are using min priotiry queue (with negLogprob)
            # #random sampling for sanity checking beam search
            # tempYprev = torch.multinomial(torch.ones(ynxt.shape[1])/ynxt.shape[1], beamSize, replacement=False).unsqueeze(dim=0).unsqueeze(dim=-1).to(device)
            # tempPscore = -1*ynxt[0,tempYprev[0,:,0],0].unsqueeze(dim=0).unsqueeze(dim=-1).to(device)
            for i in range(beamSize):
                yprevs_ = yprevs.copy()
                yprevs_.append(tempYprev[:,i,:])
                tempProb = negLogProb + tempPscore[0,i,0].item()
                prediction = (tempProb/tIter, next(pQTieBreaker), tempProb, (yprevs_, hprev, cprev))
                temp_pqueue.put(prediction)
        #keep the the top k (where k is the beamSize). Note at this point pqueue should be empty
        for i in range(beamSize):
            if temp_pqueue.qsize() == 0: break
            pqueue.put(temp_pqueue.get())
        tIter += 1
        if completedHyp_pqueue.qsize() >= maxNumHyp: break
        if pqueue.qsize() == 0: break
    if completedHyp_pqueue.qsize() == 0 and pqueue.qsize() == 0: raise Exception('Nothing found')
    elif completedHyp_pqueue.qsize() < maxNumHyp and pqueue.qsize() > 0:
        for i in range(beamSize):
            completedHyp_pqueue.put(pqueue.get())
    _, _, _, (yprevs, _, _) = completedHyp_pqueue.get() #get the best prediction
    return torch.cat(yprevs, dim=1) #1xL
