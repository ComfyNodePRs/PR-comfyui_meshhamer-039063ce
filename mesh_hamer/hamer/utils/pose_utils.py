"""
Code adapted from: https://github.com/akanazawa/hmr/blob/master/src/benchmark/eval_util.py
"""

import torch
import numpy as np
from typing import Optional, Dict, List, Tuple

def compute_similarity_transform(S1: torch.Tensor, S2: torch.Tensor) -> torch.Tensor:
    """
    Computes a similarity transform (sR, t) in a batched way that takes
    a set of 3D points S1 (B, N, 3) closest to a set of 3D points S2 (B, N, 3),
    where R is a 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    Args:
        S1 (torch.Tensor): First set of points of shape (B, N, 3).
        S2 (torch.Tensor): Second set of points of shape (B, N, 3).
    Returns:
        (torch.Tensor): The first set of points after applying the similarity transformation.
    """

    batch_size = S1.shape[0]
    S1 = S1.permute(0, 2, 1)
    S2 = S2.permute(0, 2, 1)
    # 1. Remove mean.
    mu1 = S1.mean(dim=2, keepdim=True)
    mu2 = S2.mean(dim=2, keepdim=True)
    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = (X1**2).sum(dim=(1,2))

    # 3. The outer product of X1 and X2.
    K = torch.matmul(X1, X2.permute(0, 2, 1))

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are singular vectors of K.
    U, s, V = torch.svd(K)
    Vh = V.permute(0, 2, 1)

    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[1], device=U.device).unsqueeze(0).repeat(batch_size, 1, 1)
    Z[:, -1, -1] *= torch.sign(torch.linalg.det(torch.matmul(U, Vh)))

    # Construct R.
    R = torch.matmul(torch.matmul(V, Z), U.permute(0, 2, 1))

    # 5. Recover scale.
    trace = torch.matmul(R, K).diagonal(offset=0, dim1=-1, dim2=-2).sum(dim=-1)
    scale = (trace / var1).unsqueeze(dim=-1).unsqueeze(dim=-1)

    # 6. Recover translation.
    t = mu2 - scale*torch.matmul(R, mu1)

    # 7. Error:
    S1_hat = scale*torch.matmul(R, S1) + t

    return S1_hat.permute(0, 2, 1)

def reconstruction_error(S1, S2) -> np.array:
    """
    Computes the mean Euclidean distance of 2 set of points S1, S2 after performing Procrustes alignment.
    Args:
        S1 (torch.Tensor): First set of points of shape (B, N, 3).
        S2 (torch.Tensor): Second set of points of shape (B, N, 3).
    Returns:
        (np.array): Reconstruction error.
    """
    S1_hat = compute_similarity_transform(S1, S2)
    re = torch.sqrt( ((S1_hat - S2)** 2).sum(dim=-1)).mean(dim=-1)
    return re

def eval_pose(pred_joints, gt_joints) -> Tuple[np.array, np.array]:
    """
    Compute joint errors in mm before and after Procrustes alignment.
    Args:
        pred_joints (torch.Tensor): Predicted 3D joints of shape (B, N, 3).
        gt_joints (torch.Tensor): Ground truth 3D joints of shape (B, N, 3).
    Returns:
        Tuple[np.array, np.array]: Joint errors in mm before and after alignment.
    """
    # Absolute error (MPJPE)
    mpjpe = torch.sqrt(((pred_joints - gt_joints) ** 2).sum(dim=-1)).mean(dim=-1).cpu().numpy()

    # Reconstruction_error
    r_error = reconstruction_error(pred_joints, gt_joints).cpu().numpy()
    return 1000 * mpjpe, 1000 * r_error

class Evaluator:

    def __init__(self,
                 dataset_length: int,
                 dataset: str,
                 keypoint_list: List,
                 pelvis_ind: int,
                 metrics: List = ['mode_mpjpe', 'mode_re', 'min_mpjpe', 'min_re'],
                 preds: List = ['vertices', 'keypoints_3d'],
                 pck_thresholds: Optional[List] = None):
        """
        Class used for evaluating trained models on different 3D pose datasets.
        Args:
            dataset_length (int): Total dataset length.
            keypoint_list [List]: List of keypoints used for evaluation.
            pelvis_ind (int): Index of pelvis keypoint; used for aligning the predictions and ground truth.
            metrics [List]: List of evaluation metrics to record.
        """
        self.dataset_length = dataset_length
        self.dataset = dataset
        self.keypoint_list = keypoint_list
        self.pelvis_ind = pelvis_ind
        self.metrics = metrics
        self.preds = preds
        if self.metrics is not None:
            for metric in self.metrics:
                setattr(self, metric, np.zeros((dataset_length,)))
        if self.preds is not None:
            for pred in self.preds:
                if pred == 'vertices':
                    self.vertices = np.zeros((dataset_length, 778, 3))
                if pred == 'keypoints_3d':
                    self.keypoints_3d = np.zeros((dataset_length, 21, 3))
        self.counter = 0
        if pck_thresholds is None:
            self.pck_evaluator = None
        else:
            self.pck_evaluator = EvaluatorPCK(pck_thresholds)

    def log(self):
        """
        Print current evaluation metrics
        """
        if self.counter == 0:
            print('Evaluation has not started')
            return
        print(f'{self.counter} / {self.dataset_length} samples')
        if self.pck_evaluator is not None:
            self.pck_evaluator.log()
        if self.metrics is not None:
            for metric in self.metrics:
                if metric in ['mode_mpjpe', 'mode_re', 'min_mpjpe', 'min_re']:
                    unit = 'mm'
                else:
                    unit = ''
                print(f'{metric}: {getattr(self, metric)[:self.counter].mean()} {unit}')
        print('***')

    def get_metrics_dict(self) -> Dict:
        """
        Returns:
            Dict: Dictionary of evaluation metrics.
        """
        d1 = {metric: getattr(self, metric)[:self.counter].mean() for metric in self.metrics}
        if self.pck_evaluator is not None:
            d2 = self.pck_evaluator.get_metrics_dict()
            d1.update(d2)
        return d1

    def get_preds_dict(self) -> Dict:
        """
        Returns:
            Dict: Dictionary of evaluation preds.
        """
        d1 = {pred: getattr(self, pred)[:self.counter] for pred in self.preds}
        return d1

    def __call__(self, output: Dict, batch: Dict, opt_output: Optional[Dict] = None):
        """
        Evaluate current batch.
        Args:
            output (Dict): Regression output.
            batch (Dict): Dictionary containing images and their corresponding annotations.
            opt_output (Dict): Optimization output.
        """
        if self.pck_evaluator is not None:
            self.pck_evaluator(output, batch, opt_output)

        pred_keypoints_3d = output['pred_keypoints_3d'].detach()
        pred_keypoints_3d = pred_keypoints_3d[:,None,:,:]
        batch_size = pred_keypoints_3d.shape[0]
        num_samples = pred_keypoints_3d.shape[1]
        gt_keypoints_3d = batch['keypoints_3d'][:, :, :-1].unsqueeze(1).repeat(1, num_samples, 1, 1)
        pred_vertices = output['pred_vertices'].detach()

        # Align predictions and ground truth such that the pelvis location is at the origin
        pred_keypoints_3d -= pred_keypoints_3d[:, :, [self.pelvis_ind]]
        gt_keypoints_3d -= gt_keypoints_3d[:, :, [self.pelvis_ind]]

        # Compute joint errors
        mpjpe, re = eval_pose(pred_keypoints_3d.reshape(batch_size * num_samples, -1, 3)[:, self.keypoint_list], gt_keypoints_3d.reshape(batch_size * num_samples, -1 ,3)[:, self.keypoint_list])
        mpjpe = mpjpe.reshape(batch_size, num_samples)
        re = re.reshape(batch_size, num_samples)

        # Compute 2d keypoint errors
        bbox_expand_factor = batch['bbox_expand_factor'][:,None,None,None].detach()
        pred_keypoints_2d = output['pred_keypoints_2d'].detach()
        pred_keypoints_2d = pred_keypoints_2d[:,None,:,:]*bbox_expand_factor
        gt_keypoints_2d = batch['keypoints_2d'][:,None,:,:].repeat(1, num_samples, 1, 1)*bbox_expand_factor
        conf = gt_keypoints_2d[:, :, :, -1].clone()
        kp_err = torch.nn.functional.mse_loss(
                        pred_keypoints_2d,
                        gt_keypoints_2d[:, :, :, :-1],
                        reduction='none'
                    ).sum(dim=3)
        kp_l2_loss = (conf * kp_err).mean(dim=2)
        kp_l2_loss = kp_l2_loss.detach().cpu().numpy()

        # Compute joint errors after optimization, if available.
        if opt_output is not None:
            opt_keypoints_3d = opt_output['model_joints']
            opt_keypoints_3d -= opt_keypoints_3d[:, [self.pelvis_ind]]
            opt_mpjpe, opt_re = eval_pose(opt_keypoints_3d[:, self.keypoint_list], gt_keypoints_3d[:, 0, self.keypoint_list])

        # The 0-th sample always corresponds to the mode
        if hasattr(self, 'mode_mpjpe'):
            mode_mpjpe = mpjpe[:, 0]
            self.mode_mpjpe[self.counter:self.counter+batch_size] = mode_mpjpe
        if hasattr(self, 'mode_re'):
            mode_re = re[:, 0]
            self.mode_re[self.counter:self.counter+batch_size] = mode_re
        if hasattr(self, 'mode_kpl2'):
            mode_kpl2 = kp_l2_loss[:, 0]
            self.mode_kpl2[self.counter:self.counter+batch_size] = mode_kpl2
        if hasattr(self, 'min_mpjpe'):
            min_mpjpe = mpjpe.min(axis=-1)
            self.min_mpjpe[self.counter:self.counter+batch_size] = min_mpjpe
        if hasattr(self, 'min_re'):
            min_re = re.min(axis=-1)
            self.min_re[self.counter:self.counter+batch_size] = min_re
        if hasattr(self, 'min_kpl2'):
            min_kpl2 = kp_l2_loss.min(axis=-1)
            self.min_kpl2[self.counter:self.counter+batch_size] = min_kpl2
        if hasattr(self, 'opt_mpjpe'):
            self.opt_mpjpe[self.counter:self.counter+batch_size] = opt_mpjpe
        if hasattr(self, 'opt_re'):
            self.opt_re[self.counter:self.counter+batch_size] = opt_re
        if hasattr(self, 'vertices'):
            self.vertices[self.counter:self.counter+batch_size] = pred_vertices.cpu().numpy()
        if hasattr(self, 'keypoints_3d'):
            if self.dataset == 'HO3D-VAL':
                pred_keypoints_3d = pred_keypoints_3d[:,:,[0,5,6,7,9,10,11,17,18,19,13,14,15,1,2,3,4,8,12,16,20]]
            self.keypoints_3d[self.counter:self.counter+batch_size] = pred_keypoints_3d.squeeze().cpu().numpy()

        self.counter += batch_size

        if hasattr(self, 'mode_mpjpe') and hasattr(self, 'mode_re'):
            return {
                'mode_mpjpe': mode_mpjpe,
                'mode_re': mode_re,
            }
        else:
            return {}


class EvaluatorPCK:

    def __init__(self, thresholds: List = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5],):
        """
        Class used for evaluating trained models on different 3D pose datasets.
        Args:
            thresholds [List]: List of PCK thresholds to evaluate.
            metrics [List]: List of evaluation metrics to record.
        """
        self.thresholds = thresholds
        self.pred_kp_2d = []
        self.gt_kp_2d = []
        self.gt_conf_2d = []
        self.scale = []
        self.counter = 0

    def log(self):
        """
        Print current evaluation metrics
        """
        if self.counter == 0:
            print('Evaluation has not started')
            return
        print(f'{self.counter} samples')
        metrics_dict = self.get_metrics_dict()
        for metric in metrics_dict:
            print(f'{metric}: {metrics_dict[metric]}')
        print('***')

    def get_metrics_dict(self) -> Dict:
        """
        Returns:
            Dict: Dictionary of evaluation metrics.
        """
        pcks = self.compute_pcks()
        metrics = {}
        for thr, (acc,avg_acc,cnt) in zip(self.thresholds, pcks):
            metrics.update({f'kp{i}_pck_{thr}': float(a) for i, a in enumerate(acc) if a>=0})
            metrics.update({f'kpAvg_pck_{thr}': float(avg_acc)})
        return metrics

    def compute_pcks(self):
        pred_kp_2d = np.concatenate(self.pred_kp_2d, axis=0)
        gt_kp_2d = np.concatenate(self.gt_kp_2d, axis=0)
        gt_conf_2d = np.concatenate(self.gt_conf_2d, axis=0)
        scale = np.concatenate(self.scale, axis=0)
        assert pred_kp_2d.shape == gt_kp_2d.shape
        assert pred_kp_2d[..., 0].shape == gt_conf_2d.shape
        assert pred_kp_2d.shape[1] == 1 # num_samples
        assert scale.shape[0] == gt_conf_2d.shape[0] # num_samples

        pcks = [
            self.keypoint_pck_accuracy(
                pred_kp_2d[:, 0, :, :],
                gt_kp_2d[:, 0, :, :],
                gt_conf_2d[:, 0, :]>0.5,
                thr=thr,
                scale = scale[:,None]
            )
            for thr in self.thresholds
        ]
        return pcks

    def keypoint_pck_accuracy(self, pred, gt, conf, thr, scale):
        dist = np.sqrt(np.sum((pred-gt)**2, axis=2))
        all_joints = conf>0.5
        correct_joints = np.logical_and(dist<=scale*thr, all_joints)
        pck = correct_joints.sum(axis=0)/all_joints.sum(axis=0)
        return pck, pck.mean(), pck.shape[0]

    def __call__(self, output: Dict, batch: Dict, opt_output: Optional[Dict] = None):
        """
        Evaluate current batch.
        Args:
            output (Dict): Regression output.
            batch (Dict): Dictionary containing images and their corresponding annotations.
            opt_output (Dict): Optimization output.
        """
        pred_keypoints_2d = output['pred_keypoints_2d'].detach()
        num_samples = 1
        batch_size = pred_keypoints_2d.shape[0]

        right = batch['right'].detach()
        pred_keypoints_2d[:,:,0] = (2*right[:,None]-1)*pred_keypoints_2d[:,:,0]
        box_size = batch['box_size'].detach()
        box_center = batch['box_center'].detach()
        bbox_expand_factor = batch['bbox_expand_factor'].detach()
        scale = box_size/bbox_expand_factor
        bbox_expand_factor = bbox_expand_factor[:,None,None,None]
        pred_keypoints_2d = pred_keypoints_2d*box_size[:,None,None]+box_center[:,None]
        pred_keypoints_2d = pred_keypoints_2d[:,None,:,:]
        gt_keypoints_2d = batch['orig_keypoints_2d'][:,None,:,:].repeat(1, num_samples, 1, 1)
        
        self.pred_kp_2d.append(pred_keypoints_2d[:, :, :, :2].detach().cpu().numpy())
        self.gt_conf_2d.append(gt_keypoints_2d[:, :, :, -1].detach().cpu().numpy())
        self.gt_kp_2d.append(gt_keypoints_2d[:, :, :, :2].detach().cpu().numpy())
        self.scale.append(scale.detach().cpu().numpy())

        self.counter += batch_size
