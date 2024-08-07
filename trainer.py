import datetime
import time
from turtle import forward
from typing import List, Optional, Tuple, Union
from numpy import isin
import numpy as np
import torch
import torchvision
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
from PIL import Image
import PIL.Image as PImage, PIL.ImageDraw as PImageDraw
import wandb
from tqdm import tqdm
import dist
from models import VAR, VQVAE, VectorQuantizer2
from utils.amp_sc import AmpOptimizer
from utils.misc import MetricLogger, TensorboardLogger

Ten = torch.Tensor
FTen = torch.Tensor
ITen = torch.LongTensor
BTen = torch.BoolTensor

import torchvision.transforms as T

transform = T.ToPILImage()


class VARTrainer(object):

    def __init__(self,
                 device,
                 patch_nums: Tuple[int, ...],
                 resos: Tuple[int, ...],
                 vae_local: VQVAE,
                 var_wo_ddp: VAR,
                 var: DDP,
                 var_opt: AmpOptimizer,
                 label_smooth: float,
                 logger=None,
                 cond_train=False,
                 ):
        super(VARTrainer, self).__init__()
        self.logger = logger
        self.var, self.vae_local, self.quantize_local = var, vae_local, vae_local.quantize
        self.quantize_local: VectorQuantizer2
        self.var_wo_ddp: VAR = var_wo_ddp  # after torch.compile
        self.var_opt = var_opt
        self.cond_train = cond_train
        del self.var_wo_ddp.rng
        self.var_wo_ddp.rng = torch.Generator(device=device)

        self.label_smooth = label_smooth
        self.train_loss = nn.CrossEntropyLoss(label_smoothing=label_smooth, reduction='none')
        self.val_loss = nn.CrossEntropyLoss(label_smoothing=0.0, reduction='mean')
        self.L = sum(pn * pn for pn in patch_nums)
        self.last_l = patch_nums[-1] * patch_nums[-1]
        self.loss_weight = torch.ones(1, self.L, device=device) / self.L

        self.patch_nums, self.resos = patch_nums, resos
        self.begin_ends = []
        cur = 0
        for i, pn in enumerate(patch_nums):
            self.begin_ends.append((cur, cur + pn * pn))
            cur += pn * pn

        self.prog_it = 0
        self.last_prog_si = -1
        self.first_prog = True

    @torch.no_grad()
    def eval_ep(self, ld_val: DataLoader, ep=None):
        print("Running evaludation")

        tot = 0
        L_mean, L_tail, acc_mean, acc_tail = 0, 0, 0, 0
        stt = time.time()
        training = self.var_wo_ddp.training
        self.var_wo_ddp.eval()
        for batch_data in tqdm(ld_val):
            if isinstance(batch_data, dict):
                inp_B3HW = batch_data["image"]
                label_B = batch_data["label"]
                inpaint_B3HW = batch_data["inpaint_image"]
                cloth_B3HW = batch_data["cloth_pure"]
            else:
                inp_B3HW, label_B = batch_data
            B, V = label_B.shape[0], self.vae_local.vocab_size
            inp_B3HW = inp_B3HW.to(dist.get_device(), non_blocking=True)
            label_B = label_B.to(dist.get_device(), non_blocking=True)

            if self.cond_train:
                inpaint_B3HW = inpaint_B3HW.to(dist.get_device(), non_blocking=True)
                cloth_B3HW = cloth_B3HW.to(dist.get_device(), non_blocking=True)

            gt_idx_Bl: List[ITen] = self.vae_local.img_to_idxBl(inp_B3HW)  # [(B, patch)] * 10
            inpaint_idx_Bl: List[ITen] = self.vae_local.img_to_idxBl(inpaint_B3HW)
            cloth_idx_Bl = self.vae_local.img_to_idxBl(cloth_B3HW)

            gt_BL = torch.cat(gt_idx_Bl, dim=1)
            x_BLCv_wo_first_l: Ten = self.quantize_local.idxBl_to_var_input(gt_idx_Bl)
            if self.cond_train:
                inpaint_BLCv: Ten = self.quantize_local.idxBl_to_var_input(inpaint_idx_Bl)
                cloth_BLCv: Ten = self.quantize_local.idxBl_to_var_input(cloth_idx_Bl)

            self.var_wo_ddp.forward
            logits_BLV = self.var_wo_ddp.forward(label_B, x_BLCv_wo_first_l)
            L_mean += self.val_loss(logits_BLV.data.view(-1, V), gt_BL.view(-1)) * B
            L_tail += self.val_loss(logits_BLV.data[:, -self.last_l:].reshape(-1, V),
                                    gt_BL[:, -self.last_l:].reshape(-1)) * B
            acc_mean += (logits_BLV.data.argmax(dim=-1) == gt_BL).sum() * (100 / gt_BL.shape[1])
            acc_tail += (logits_BLV.data[:, -self.last_l:].argmax(dim=-1)
                         == gt_BL[:, -self.last_l:]).sum() * (100 / self.last_l)
            tot += B
            # added validation 
            with torch.inference_mode():
                with torch.autocast('cuda', enabled=True, dtype=torch.float16,
                                    cache_enabled=True):  # using bfloat16 can be faster
                    recon_B3HW = self.var_wo_ddp.autoregressive_infer_cfg(B=B,
                                                                          label_B=label_B,
                                                                          cfg=1,
                                                                          top_k=900,
                                                                          top_p=0.95,
                                                                          g_seed=0,
                                                                          more_smooth=False)
            if self.logger is not None:
                formatted_images = []
                for i in range(inp_B3HW.shape[0]):
                    gt = (inp_B3HW[i] + 1) / 2
                    formatted_images.append(wandb.Image(transform(gt.cpu()), caption=f"GT/{label_B[i]}"))
                    recon_img = recon_B3HW[i]
                    formatted_images.append(wandb.Image(transform(recon_img.cpu()),
                                                        caption=f"reconstruct/{label_B[i]}"))
                self.logger.log({"validation": formatted_images, "epoch": ep})

        self.var_wo_ddp.train(training)

        stats = L_mean.new_tensor([L_mean.item(), L_tail.item(), acc_mean.item(), acc_tail.item(), tot])
        dist.allreduce(stats)
        tot = round(stats[-1].item())
        stats /= tot
        L_mean, L_tail, acc_mean, acc_tail, _ = stats.tolist()
        return L_mean, L_tail, acc_mean, acc_tail, tot, time.time() - stt

    def train_step(
        self,
        it: int,
        g_it: int,
        stepping: bool,
        metric_lg: MetricLogger,
        tb_lg: TensorboardLogger,
        inp_B3HW: FTen,
        label_B: Union[ITen, FTen],
        prog_si: int,
        prog_wp_it: float,
    ) -> Tuple[Optional[Union[Ten, float]], Optional[float]]:
        # if progressive training
        self.var_wo_ddp.prog_si = self.vae_local.quantize.prog_si = prog_si
        if self.last_prog_si != prog_si:
            if self.last_prog_si != -1:
                self.first_prog = False
            self.last_prog_si = prog_si
            self.prog_it = 0
        self.prog_it += 1
        prog_wp = max(min(self.prog_it / prog_wp_it, 1), 0.01)
        if self.first_prog:
            prog_wp = 1  # no prog warmup at first prog stage, as it's already solved in wp
        if prog_si == len(self.patch_nums) - 1:
            prog_si = -1  # max prog, as if no prog

        # forward
        B, V = label_B.shape[0], self.vae_local.vocab_size
        self.var.require_backward_grad_sync = stepping

        # TODO: VAE
        gt_idx_Bl: List[ITen] = self.vae_local.img_to_idxBl(inp_B3HW)
        gt_BL = torch.cat(gt_idx_Bl, dim=1)
        x_BLCv_wo_first_l: Ten = self.quantize_local.idxBl_to_var_input(gt_idx_Bl)

        with self.var_opt.amp_ctx:
            self.var_wo_ddp.forward
            logits_BLV = self.var(label_B, x_BLCv_wo_first_l)
            loss = self.train_loss(logits_BLV.view(-1, V), gt_BL.view(-1)).view(B, -1)
            if prog_si >= 0:  # in progressive training
                bg, ed = self.begin_ends[prog_si]
                assert logits_BLV.shape[1] == gt_BL.shape[1] == ed
                lw = self.loss_weight[:, :ed].clone()
                lw[:, bg:ed] *= min(max(prog_wp, 0), 1)
            else:  # not in progressive training
                lw = self.loss_weight
            loss = loss.mul(lw).sum(dim=-1).mean()

        # backward
        grad_norm, scale_log2 = self.var_opt.backward_clip_step(loss=loss, stepping=stepping)

        # log
        pred_BL = logits_BLV.data.argmax(dim=-1)
        if it == 0 or it in metric_lg.log_iters:
            Lmean = self.val_loss(logits_BLV.data.view(-1, V), gt_BL.view(-1)).item()
            acc_mean = (pred_BL == gt_BL).float().mean().item() * 100
            if prog_si >= 0:  # in progressive training
                Ltail = acc_tail = -1
            else:  # not in progressive training
                Ltail = self.val_loss(logits_BLV.data[:, -self.last_l:].reshape(-1, V),
                                      gt_BL[:, -self.last_l:].reshape(-1)).item()
                acc_tail = (pred_BL[:, -self.last_l:] == gt_BL[:, -self.last_l:]).float().mean().item() * 100
            grad_norm = grad_norm.item()
            metric_lg.update(Lm=Lmean, Lt=Ltail, Accm=acc_mean, Acct=acc_tail, tnm=grad_norm)

        # log to tensorboard
        if g_it == 0 or (g_it + 1) % 500 == 0:
            prob_per_class_is_chosen = pred_BL.view(-1).bincount(minlength=V).float()
            dist.allreduce(prob_per_class_is_chosen)
            prob_per_class_is_chosen /= prob_per_class_is_chosen.sum()
            cluster_usage = (prob_per_class_is_chosen > 0.001 / V).float().mean().item() * 100
            if dist.is_master():
                # if g_it == 0:
                # tb_lg.log({'AR_iter_loss': cluster_usage})
                # tb_lg.log({'AR_iter_loss': cluster_usage})
                kw = dict(z_voc_usage=cluster_usage)
                for si, (bg, ed) in enumerate(self.begin_ends):
                    if 0 <= prog_si < si:
                        break
                    pred, tar = logits_BLV.data[:, bg:ed].reshape(-1, V), gt_BL[:, bg:ed].reshape(-1)
                    acc = (pred.argmax(dim=-1) == tar).float().mean().item() * 100
                    ce = self.val_loss(pred, tar).item()
                    kw[f'acc_{self.resos[si]}'] = acc
                    kw[f'L_{self.resos[si]}'] = ce
                tb_lg.log({'AR_iter_loss': kw})
                tb_lg.log({'AR_iter_schedule': self.resos[prog_si]})

        self.var_wo_ddp.prog_si = self.vae_local.quantize.prog_si = -1
        return grad_norm, scale_log2

    def get_config(self):
        return {
            'patch_nums': self.patch_nums,
            'resos': self.resos,
            'label_smooth': self.label_smooth,
            'prog_it': self.prog_it,
            'last_prog_si': self.last_prog_si,
            'first_prog': self.first_prog,
        }

    def state_dict(self):
        state = {'config': self.get_config()}
        for k in ('var_wo_ddp', 'vae_local', 'var_opt'):
            m = getattr(self, k)
            if m is not None:
                if hasattr(m, '_orig_mod'):
                    m = m._orig_mod
                state[k] = m.state_dict()
        return state

    def load_state_dict(self, state, strict=True, skip_vae=False):
        for k in ('var_wo_ddp', 'vae_local', 'var_opt'):
            if skip_vae and 'vae' in k:
                continue
            m = getattr(self, k)
            if m is not None:
                if hasattr(m, '_orig_mod'):
                    m = m._orig_mod
                ret = m.load_state_dict(state[k], strict=strict)
                if ret is not None:
                    missing, unexpected = ret
                    print(f'[VARTrainer.load_state_dict] {k} missing:  {missing}')
                    print(f'[VARTrainer.load_state_dict] {k} unexpected:  {unexpected}')

        config: dict = state.pop('config', None)
        self.prog_it = config.get('prog_it', 0)
        self.last_prog_si = config.get('last_prog_si', -1)
        self.first_prog = config.get('first_prog', True)
        if config is not None:
            for k, v in self.get_config().items():
                if config.get(k, None) != v:
                    err = f'[VAR.load_state_dict] config mismatch:  this.{k}={v} (ckpt.{k}={config.get(k, None)})'
                    if strict:
                        raise AttributeError(err)
                    else:
                        print(err)
