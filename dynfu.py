import os
import argparse
import numpy as np
import torch
import trimesh
import warnings
from functools import partial 
from matplotlib import pyplot as plt

warnings.simplefilter("ignore", UserWarning)
from fusion import TSDFVolumeTorch
from dataset.ourdataset import OurDataset
from tracker import ICPTracker
from utils import load_config, get_volume_setting, get_time
from scipy.spatial import KDTree
from tmp_utils import uniform_sample
from tmp_utils import (
    dqnorm,
    custom_transpose_batch,
    get_W,
    render_depth,
    warp_helper,
    warp_to_live_frame,
    plot_vis_depthmap,
    SE3_dq,
    plot_heatmap_step,
    plot_vis_activemap,
    get_vmap_make_dq_from_vec,
    # inverse_dq_to_vec
)
import time
from functorch import vmap, jacrev
from icp import compute_normal, compute_vertex
from typing import Container, List, Set, Dict, Tuple, Optional, Union, Any

# 从8维对偶四元数提取旋转向量 theta 和平移向量 t
def inverse_dq_to_vec(dq: torch.tensor) -> torch.tensor:
    """
    从8维对偶四元数 dq 提取旋转向量 theta 和平移向量 t
    返回一个6维向量，前3维是旋转，后3维是平移
    """
    # 提取旋转向量 theta
    # theta = extract_theta_from_q(dq[None][0][0:4])
    # 计算旋转角度的模
    theta_norm = 2 * [torch.acos(dq[0])[None]][0]  # 旋转角度的模
    
    # 避免 theta_norm == 0 的情况，用 where 代替 if 语句
    sin_half_theta = torch.sqrt([torch.clamp(1 - dq[0]**2, min=1e-6)[None]][0])  # 防止除以零
    axis = torch.stack([dq[1], dq[2], dq[3]]) / sin_half_theta
    
    # 如果 theta_norm 为零，旋转向量应该为零向量
    theta = (theta_norm * axis)
    theta = (torch.where(theta_norm == 0, [torch.zeros(3)[None]][0].to(dq), theta)).squeeze()
    
    # 提取平移向量 t
    # translation = extract_translation_from_dual(dq[None][0])
    translation = 2 * torch.stack([
        -dq[4]*dq[1] + dq[5]*dq[0] - dq[6]*dq[3] + dq[7]*dq[2],
        -dq[4]*dq[2] + dq[5]*dq[3] + dq[6]*dq[0] - dq[7]*dq[1],
        -dq[4]*dq[3] - dq[5]*dq[2] + dq[6]*dq[1] + dq[7]*dq[0]
    ]).squeeze()
    print(theta.shape, translation.shape)
    # 合并为一个6维向量
    return torch.cat([theta, translation])

def data_term(
    Xw: torch.tensor,
    Tlw: torch.tensor,
    K: torch.tensor,
    dgv: torch.tensor,
    dgse: torch.tensor,
    dgw: torch.tensor,
    node_to_nn: torch.tensor,
    vertex0,
    normal0,
    mask0
) -> torch.tensor:
    """_summary_

    Args:
        Xc (torch.tensor): _description_
        Tlw (torch.tensor): _description_
        dgv (torch.tensor): _description_
        dgse (torch.tensor): _description_
        dgw (torch.tensor): _description_
        node_to_nn (torch.tensor): _description_

    Returns:
        torch.tensor: _description_
    """
    # print(Xc,nc,gt_v,gt_n)
    H, W, C = vertex0.shape
    Xt = warp_helper(Xw, Tlw, dgv, dgse, dgw, node_to_nn)
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    u_ = torch.round((Xt[0] / Xt[2]) * fx + cx).long()  # [h, w]
    v_ = torch.round((Xt[1] / Xt[2]) * fy + cy).long()  # [h, w]
    inviews = (u_ > 0) & (u_ < W-1) & (v_ > 0) & (v_ < H-1)
    u_ = torch.clamp(u_, 0, W - 1)
    v_ = torch.clamp(v_, 0, H - 1)
    v = [v_[None]][0]
    u = [u_[None]][0]
    # isValid  = mask0[v,u]>0
    gt_x = vertex0[v,u].squeeze()
    gt_n = normal0[v,u].squeeze()
    # 根据isValid计算，如果不合法，gt_x的第三维变为0
    # gt_x = gt_x * isValid.view(-1,1).repeat(1,3)

    # if you want it to similar to surfel warp, uncomment the lines below
    print(gt_n.shape,gt_x.shape,Xt.shape)
    e = inviews*(gt_n.dot(gt_x - Xt).view(1, 1) ** 2 )
    # print("Xt:", Xt,Xt.shape)
    # print("gt_v - Xt:", gt_v - Xt)
    # print("Dot product (e):", e)
    re = e
    # or using tukey like in DynFu paper.
    # e = gt_n.dot(Xt - gt_v).view(1,1)
    # re = robust_Tukey_penalty(e, 0.01)
    return re


def reg_term(
    Tlw: torch.tensor,
    dgv: torch.tensor,
    dgse: torch.tensor,
    dgw: torch.tensor,
    dgv_nn: torch.tensor,
) -> torch.tensor:
    """Regularization term. This method computes the as-rigid-as-possible energy described in the dynamic fusion paper.
    However, the original equation uses sum only, which potentially causes exploding gradient, so I use mean instead of sum. Note that the scalar \alpha_ij is merged outside. In the paper, it is 200. With my setting is 0.025 radius, for example, so the weight outside is 5.

    Eq (8): mean(T_ic dg_v^j - T_jc dg_v^j)

    Args:
        Tlw (torch.tensor)      : Shape (4,4).  The explicit rigid transformation.
        dgv (torch.tensor)      : Shape (N,3).  The nodes position.
        dgse (torch.tensor)     : Shape (N,8).  The se3 transformation represented as dual quaternion of the nodes.
        dgw (torch.tensor)      : Shape (N,1).  The influential radius of the nodes.
        dgv_nn (torch.tensor)   : Shape (N,4).  The index list of the nodes' neighbors

    Returns:
        torch.tensor:           : Shape (1).    Return the scalar result from the mentioned Eq.
    """
    shape = dgv.shape
    knn = dgv_nn.shape[1]
    # self warp
    # print('reg term self warp')
    warp_helper_vmap = vmap(warp_helper, in_dims=(0, None, None, None, None, 0))
    dgv_warped = warp_helper_vmap(dgv, Tlw, dgv, dgse, dgw, dgv_nn)

    # self warp with neighbors' se3
    # print('reg term warp with neighbors')
    dgv_nn_nn = dgv_nn[dgv_nn].view(-1, knn)
    dgv_rep = dgv.view(-1, 1, 3).repeat_interleave(knn, 1).view(-1, 3)
    dgv_nn_warp = warp_helper_vmap(dgv_rep, Tlw, dgv, dgse, dgw, dgv_nn_nn).view(
        -1, knn, 3
    )
    dgv_warped_rep = dgv_warped.view(-1, 1, 3).repeat_interleave(knn, 1)
    el2 = torch.linalg.norm(dgv_nn_warp - dgv_warped_rep, dim=2)**2
    # print(el2)
    ssel2 = el2.mean()
    return ssel2


def energy(
    Xw: torch.tensor,
    Tlw: torch.tensor,
    K: torch.tensor,
    dgv: torch.tensor,
    dgse: torch.tensor,
    dgw: torch.tensor,
    node_to_nn: torch.tensor,
    dgv_nn: torch.tensor,
    vertex0: torch.tensor,
    normal0: torch.tensor,
    mask0: torch.tensor,
) -> Tuple[torch.tensor, torch.tensor]:
    """_summary_

    Args:
        gt_Xc (torch.tensor): _description_
        Tlw (torch.tensor): _description_
        dgv (torch.tensor): _description_
        dgse (torch.tensor): _description_
        dgw (torch.tensor): _description_
        node_to_nn (torch.tensor): _description_
        dgv_nn (torch.tensor): _description_

    Returns:
        Tuple[torch.tensor, torch.tensor]: _description_
    """
    data_vmap = vmap(data_term, in_dims=(0, None, None,None, None, None, 0,None,None,None))
    make_dq_from_vec_vmap = get_vmap_make_dq_from_vec()
    dgse_dq = make_dq_from_vec_vmap(dgse)
    data_val_tmp = data_vmap(Xw, Tlw, K,dgv, dgse_dq, dgw, node_to_nn,vertex0,normal0,mask0)
    # data_val_tmp2 = torch.where(torch.tensor(data_val_tmp >1e-5),data_val_tmp, torch.tensor(0.0).type_as(data_val_tmp))
    # data_val = data_val_tmp2.sum() / torch.tensor(data_val_tmp >1e-5).sum()
    data_val = data_val_tmp.mean()
    reg_val = reg_term(Tlw, dgv, dgse_dq, dgw, dgv_nn)
    re = ((data_val + 5*reg_val) / 2).float() # 5 = lambda in surfelwarp paper
    # re = data_val
    return re, re


def optim_energy(
    depth0: torch.tensor,
    depth_map: torch.tensor,
    normal_map: torch.tensor,
    vertex_map: torch.tensor,
    Tlw: torch.tensor,
    dgv: torch.tensor,
    dgse: torch.tensor,
    dgw: torch.tensor,
    kdtree: Any,
    K: torch.tensor,
    _knn: int,
    plot_func_dict: Any
) -> torch.tensor:
    """_summary_

    Args:
        depth0 (torch.tensor): _description_
        depth_map (torch.tensor): _description_
        normal_map (torch.tensor): _description_
        vertex_map (torch.tensor): _description_
        Tlw (torch.tensor): _description_
        dgv (torch.tensor): _description_
        dgse (torch.tensor): _description_
        dgw (torch.tensor): _description_
        kdtree (Any): _description_
        K (torch.tensor): _description_
        _knn (int): _description_
        plot_func_dict (Any): _description_

    Returns:
        torch.tensor: _description_
    """
    # RED for ground truth  
    RED = (1,0,0)
    # YELLOW for predicted / our projection  
    YELLOW = (1,1,0)
    # GREEN for both active 
    GREEN = (0,1, 0)
    knn = _knn
    vertex0 = compute_vertex(depth0, K)
    normal0 = compute_normal(vertex0)
    mask0 = depth0 > 0.0
    mask_hat = depth_map > 0.0
    # 根据mask_hat筛选vertex_map, normal_map
    vertex_cam = vertex_map[mask_hat]
    # assert mask_hat.shape == (480, 640)
    t1 = time.time()
    H, W = mask0.shape
    activated_map = torch.zeros(H,W,3)
    res = []
    node_to_nn = []
    dists_nn=[]
    vertex_nn=[]
    w2c = torch.inverse(Tlw).to(vertex_map.device)
    # 将vertex_map转换到世界坐标系
    vertex_world = (w2c[:3, :3] @ vertex_cam.T + w2c[:3, 3].view(3, 1)).T
    # 查询kdtree获取这些vertex_world的k近邻dgv
    dists, idx = kdtree.query(vertex_world.cpu(), k=knn, workers=-1)
    idx = torch.tensor(idx)
    # 如果有缺失的邻居，跳过该点
    valid_mask = ~(idx == dgv.shape[0]).any(dim=1)
    idx_valid = idx[valid_mask]
    vertex_world_valid = vertex_world[valid_mask]
    node_to_nn = idx_valid.to(vertex_map.device)




    # 扫描帧的非零合法图索引(注意是二维，分别是行与列)
    # mask0_idx  = (mask0 ).nonzero(as_tuple=False)
    # # 全局帧的非零合法图索引
    # mask_hat_idx = (mask_hat  ).nonzero(as_tuple=False)
    # # 扫描帧对应的全红
    # activated_map[mask0_idx[:,0],mask0_idx[:,1] ] = torch.tensor(RED).float()
    # # 全局帧对应的全黄
    # activated_map[mask_hat_idx[:,0],mask_hat_idx[:,1] ] = torch.tensor(YELLOW).float()
    # # map onto: depth0  -> our.  
    # anchor_mask =  mask_hat_idx
    # onto_mask =   mask0_idx
    # # 构造全局帧的kdtree
    # kdtree2d = KDTree(anchor_mask.cpu())
    # # 在全局帧图寻找当前扫描帧对应的最近合法点
    # d, ii = kdtree2d.query(onto_mask.cpu(), k=1, workers=-1)
    # ii = ii[ii != anchor_mask.shape[0]]
    # # 对应的全局帧中合法且近邻的索引
    # anchor_mask_mapped_idx = anchor_mask[ii]
    # 标记为绿色
    # activated_map[anchor_mask_mapped_idx[:,0],anchor_mask_mapped_idx[:,1] ] = torch.tensor(GREEN).float()
    # 遍历这些索引对应的像素，寻找当前扫描帧对应的全局帧的最近邻点的三维坐标以及相邻的4个最近关键点
    # for i in range(anchor_mask_mapped_idx.shape[0]):
    #     y0,x0 = onto_mask[i]
    #     y,x = anchor_mask_mapped_idx[i]
    #     if y0 == y and x == x0:
    #         continue
    #     activated_map[y,x] = torch.tensor(GREEN).float()
    #     # Convert the vertex_map point from camera coordinates to world coordinates
    #     vertex_cam = vertex_map[y, x]
    #     vertex_world = (w2c[:3, :3] @ vertex_cam + w2c[:3, 3]).cpu()
    #     dists, idx = kdtree.query(vertex_world, k=knn, workers=-1)
    #     # If there are missing neighbors, skip the point
    #     if dgv.shape[0] in idx:
    #         continue
    #     # vertex_nn存储vertex_map[y,x]以及其knn个邻居的坐标
    #     tmp=[]
    #     tmp.append(vertex_world)
    #     for idxx in idx:
    #         tmp.append(dgv[idxx])
    #     vertex_nn.append(tmp)
    #     dists_nn.append(dists)
    #     node_to_nn.append(torch.tensor(idx).type_as(vertex_map).long())
    #     res.append(
    #         torch.stack(
    #             [
    #                 vertex0[y0, x0],
    #                 normal0[y0, x0],
    #                 vertex_world.to(vertex_map.device),
    #                 # vertex_map[y, x],
    #                 normal_map[y, x],
    #             ]
    #         )
    #     )
    # # map onto: our -> depth0  . 
    # anchor_mask =   mask0_idx
    # onto_mask =  mask_hat_idx
    # kdtree2d = KDTree(anchor_mask.cpu())
    # d, ii = kdtree2d.query(onto_mask.cpu(), k=1, workers=-1)
    # ii = ii[ii != anchor_mask.shape[0]]
    # anchor_mask_mapped_idx = anchor_mask[ii]
    # # activated_map[anchor_mask_mapped_idx[:,0],anchor_mask_mapped_idx[:,1] ] = torch.tensor(GREEN).float()
    # for i in range(anchor_mask_mapped_idx.shape[0]):
    #     y0,x0 = anchor_mask_mapped_idx[i]
    #     y,x =  onto_mask[i]
    #     if y0 == y and x == x0:
    #         continue
    #     activated_map[y0,x0] = torch.tensor(GREEN).float()
    #     # dists, idx = kdtree.query(vertex_map[y, x].cpu(), k=knn, workers=-1)
    #     vertex_cam = vertex_map[y, x]
    #     vertex_world = (w2c[:3, :3] @ vertex_cam + w2c[:3, 3]).cpu()
    #     dists, idx = kdtree.query(vertex_world, k=knn, workers=-1)
    #     # If there are missing neighbors, skip the point
    #     if dgv.shape[0] in idx:
    #         continue
    #     tmp=[]
    #     tmp.append(vertex_world)
    #     for idxx in idx:
    #         tmp.append(dgv[idxx])
    #     vertex_nn.append(tmp)
    #     dists_nn.append(dists)
    #     node_to_nn.append(torch.tensor(idx).type_as(vertex_map).long())
    #     res.append(
    #         torch.stack(
    #             [
    #                 vertex0[y0, x0],
    #                 normal0[y0, x0],
    #                 vertex_world.to(vertex_map.device),
    #                 # vertex_map[y, x],
    #                 normal_map[y, x],
    #             ]
    #         )
    #     )

    # print("done corrs: ", len(res), time.time()-t1)
    # plot_func_dict['plot_activemap'](activation_map=activated_map)
    # node_to_nn = torch.stack(node_to_nn)
    dists, idx = kdtree.query(dgv.cpu(), k=range(2, 2 + knn), workers=-1)
    dgv_nn = torch.tensor(idx).type_as(dgv).long()
    # res = torch.stack(res)
    # assert len(res.shape) == 3  # filtered_dim, 4, 3
    assert len(node_to_nn.shape) == 2  # filtered_dim, 4
    energy_jac = jacrev(energy, argnums=4, has_aux=True)
    dqnorm_vmap = vmap(dqnorm, in_dims=0)
    # print("Start optimize! ")
    I = torch.eye(6).type_as(Tlw)  # to counter not full rank
    bs = 1  # res.shape[0]
    lr = 1.0
    old_loss = 999
    patient = 0
    aggressive = 0
    backup_dgse = None
    for i in range(5):
        t1 = time.time()
        print("start compute jse3")
        jse3, fx = energy_jac(vertex_world_valid, Tlw,K, dgv, dgse, dgw, node_to_nn, dgv_nn,vertex0,normal0,mask0)
        if torch.sum(fx) < 1e-5:
            break
        # lmda = torch.mean(jse3.abs()) 
        lmda = torch.mean(jse3.abs(), dim = 1) + 1e-8
        # print("done se3")
        j = jse3.view(bs, len(dgv), 1, 6)  # [bs,n_node,1,6]
        jT = custom_transpose_batch(j, isknn=True)  # [bs,n_node, 6,1]
        tmp_A = torch.einsum("bnij,bnjl->bnil", jT, j).view(
            bs * len(dgv), 6, 6
        )  # [bs*n_node,6,6]
        # plot_func_dict['plot_heatmap'](a=tmp_A.view(-1,6,6))
        # A = (tmp_A + lmda * I.view(1, 1, 6, 6)).view(
        #     bs * len(dgv), 6, 6
        # )  #  [bs*n_node,6,6]
        A = (tmp_A + torch.einsum('b,ij -> bij',lmda, I.view( 6, 6))).view(
            bs * len(dgv), 6, 6
        )  #  [bs*n_node,6,6]
        b = torch.einsum("bnij,bj->bni", jT, fx.view(bs, 1)).view(
            bs * len(dgv), 6, 1
        )  # [bs*n_node, 6, 1]
        # A = torch.linalg.cholesky(A)
        # solved_delta = torch.cholesky_solve(b, A)
        # solved_delta = torch.linalg.lstsq(A, b)
        solved_delta = torch.linalg.solve(A, b)
        # solved_delta = solved_delta.solution.view(bs, len(dgv), 6).mean(dim=0)
        solved_delta = solved_delta.view(bs, len(dgv), 6).mean(dim=0)
        # eliminate nan and inf
        solved_delta = torch.where(
            torch.any(torch.isnan(solved_delta.view(dgse.shape))).view(-1, 1),
            torch.zeros(6).type_as(dgse),
            solved_delta.view(dgse.shape),
        )
        solved_delta = torch.where(
            torch.any(torch.isinf(solved_delta.view(dgse.shape))).view(-1, 1),
            torch.zeros(6).type_as(dgse),
            solved_delta.view(dgse.shape),
        )
        # update
        backup_dgse = dgse.clone()
        dgse -=  lr*solved_delta
        # dgse = dqnorm_vmap(dgse.view(-1, 8)).view(len(dgv), 8)
        plot_func_dict['plot_dgse'](a=dgse.view(1,-1,6))
        print(
            "log: ",
            torch.sum(fx),
            lr,
            lmda.mean(), # i don't need to see all samples :'( 
            # torch.min(jse3[jse3.abs() > 0].abs()),
            torch.mean(jse3),
            torch.mean(jse3.abs()),
            time.time() - t1
        )
        if old_loss == 999:
            old_loss = torch.sum(fx)
        elif old_loss < torch.sum(fx):
            patient += 1 
            if patient == 3:
                dgse = backup_dgse
                patient = 0 
                lr = lr/2
        elif old_loss > torch.sum(fx):
            old_loss = torch.sum(fx) 
            aggressive += 1
            if aggressive == 3:
                aggressive = 0
                lr = min(lr*2,8.0)
    return dgse.clone(), torch.sum(fx)


class DynFu:
    """_summary_
    """    
    def __init__(self, args: Any) -> None:
        """This function will initialize all necessary parameters.

        Args:
            args (Any): _description_
        """        
        self.subsample_rate = 5.0
        self.args = args
        self.knn = 4
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # device = torch.device("cpu")
        self.dataset = OurDataset(
            os.path.join(args.data_root),
            self.device,
            near=args.near,
            far=args.far,
            img_scale=0.25,
        )
        self.vol_dims, self.vol_origin, self.voxel_size = get_volume_setting(args)
        self.tsdf_volume = TSDFVolumeTorch(
            self.vol_dims,
            self.vol_origin,
            self.voxel_size,
            self.device,
            margin=3,
            fuse_color=args.fuse_color,
        )
        self.icp_tracker = ICPTracker(args, self.device)

    def process(self) -> None:
        """_summary_
        """        
        H, W = self.dataset.H, self.dataset.W
        t, poses, poses_gt = list(), list(), list()
        Tlw, depth1, color1 = None, None, None
        make_dq_from_vec_vmap = get_vmap_make_dq_from_vec()
        for i_ in range(0, len(self.dataset), 1):
            # start from frame 20
            i = i_- 49
            if i <0: continue
            t0 = get_time()
            sample = self.dataset[i_]
            color0, depth0, pose_gt, K = sample  # use live image as template image (0)
            plot_vis_depthmap(depth0, f"{args.save_dir}/vis_gt", i)

            if i == 0:  # initialize
                Tlw = pose_gt
            else:  # tracking
                print("Start optimize! ")
                verts, faces, norms = self.tsdf_volume.get_mesh(istorch=True)
                # 获取verts在xyz维度的最大最小值与平均值
                # x_min, x_max = verts[:, 0].min(), verts[:, 0].max()
                # x_mean = verts[:, 0].mean()
                # y_min, y_max = verts[:, 1].min(), verts[:, 1].max()
                # y_mean = verts[:, 1].mean()
                # z_min, z_max = verts[:, 2].min(), verts[:, 2].max()
                # z_mean = verts[:, 2].mean()
                # print(f"verts x_min: {x_min}, x_max: {x_max}")
                # print(f"verts x_mean: {x_mean}")
                # print(f"verts y_min: {y_min}, y_max: {y_max}")
                # print(f"verts y_mean: {y_mean}")
                # print(f"verts z_min: {z_min}, z_max: {z_max}")
                # print(f"verts z_mean: {z_mean}")
                partial_tsdf = trimesh.Trimesh(
                    vertices=verts, faces=faces, vertex_normals=norms
                )
                partial_tsdf.export(
                    os.path.join(args.save_dir, f"mesh_{str(i).zfill(6)}.obj")
                )
                for j in range(3):

                    Tlw_i = torch.inverse(Tlw).to(self.device)
                    # warp vertices to live frame
                    dgse_dq = make_dq_from_vec_vmap(self.dgse)
                    verts_warp = warp_to_live_frame(
                        verts, Tlw_i, self.dgv, dgse_dq, self.dgw, self._kdtree
                    )
                    # 获取verts_warp在xyz维度的最大最小值
                    # x_min, x_max = verts_warp[:, 0].min(), verts_warp[:, 0].max()
                    # # 计算平均值
                    # x_mean = verts_warp[:, 0].mean()
                    # y_min, y_max = verts_warp[:, 1].min(), verts_warp[:, 1].max()
                    # y_mean = verts_warp[:, 1].mean()
                    # z_min, z_max = verts_warp[:, 2].min(), verts_warp[:, 2].max()
                    # z_mean = verts_warp[:, 2].mean()
                    # print(f"verts_warp x_min: {x_min}, x_max: {x_max}")
                    # print(f"verts_warp x_mean: {x_mean}")
                    # print(f"verts_warp y_min: {y_min}, y_max: {y_max}")
                    # print(f"verts_warp y_mean: {y_mean}")
                    # print(f"verts_warp z_min: {z_min}, z_max: {z_max}")
                    # print(f"verts_warp z_mean: {z_mean}")

                
                    partial_tsdf = trimesh.Trimesh(
                        vertices=verts_warp, faces=faces, vertex_normals=norms
                    )
                    partial_tsdf.export(
                        os.path.join(args.save_dir, f"warped_mesh_{str(i).zfill(6)}.obj")
                    )
                    # render depth, vertex and normal
                    image_size = [H, W]
                    # we already use Tlw_i in warping, so no need of passing it into rendering function
                    pose_tmp = torch.eye(4).to(self.device)
                    depth_map, normal_map, vertex_map = render_depth(
                        pose_tmp[:3, :3].view(1, 3, 3),
                        pose_tmp[:3, 3].view(1, 3),
                        K.view(1, 3, 3),
                        verts_warp,
                        faces,
                        image_size,
                        self.device,
                    )
                    # 获取vertex_map在xyz维度的最大最小值
                    # x_min, x_max = vertex_map[:, :, 0].min(), vertex_map[:, :, 0].max()
                    # x_mean = vertex_map[:, :, 0].mean()
                    # y_min, y_max = vertex_map[:, :, 1].min(), vertex_map[:, :, 1].max()
                    # y_mean = vertex_map[:, :, 1].mean()
                    # z_min, z_max = vertex_map[:, :, 2].min(), vertex_map[:, :, 2].max()
                    # z_mean = vertex_map[:, :, 2].mean()
                    # print(f"vertex_map x_min: {x_min}, x_max: {x_max}")
                    # print(f"vertex_map x_mean: {x_mean}")
                    # print(f"vertex_map y_min: {y_min}, y_max: {y_max}")
                    # print(f"vertex_map y_mean: {y_mean}")
                    # print(f"vertex_map z_min: {z_min}, z_max: {z_max}")
                    # print(f"vertex_map z_mean: {z_mean}")

                    plot_vis_depthmap(depth_map, f"{args.save_dir}/vis", i)
                    if j %1 ==0:
                        T10 = self.icp_tracker(depth0, depth_map, K)  # transform from 0 to 1
                        Tlw = Tlw @ T10
                        # after rigid icp, make sure to render depth again. 
                        Tlw_i = torch.inverse(Tlw).to(self.device)
                        verts_warp = warp_to_live_frame(
                            verts, Tlw_i, self.dgv, dgse_dq, self.dgw, self._kdtree
                        )
                        # 用Tlw_i将verts_warp还原回原始坐标系
                        # verts_origin = Tlw.type_as(verts_warp)[:3, :3] @ verts_warp.T + Tlw.type_as(verts_warp)[:3, 3].view(3, 1)
                        # verts_origin = verts_origin.T
                        # partial_tsdf = trimesh.Trimesh(
                        #     vertices=verts_origin, faces=faces, vertex_normals=norms
                        # )
                        # partial_tsdf.export(
                        #     os.path.join(args.save_dir, f"origin_mesh_{str(i).zfill(6)}.obj")
                        # )
                        # render depth, vertex and normal
                        image_size = [H, W]
                        # we already use Tlw_i in warping, so no need of passing it into rendering function
                        pose_tmp = torch.eye(4).to(self.device)
                        depth_map, normal_map, vertex_map = render_depth(
                            pose_tmp[:3, :3].view(1, 3, 3),
                            pose_tmp[:3, 3].view(1, 3),
                            K.view(1, 3, 3),
                            verts_warp,
                            faces,
                            image_size,
                            self.device,
                        )
                    # optim energy and set dgse
                    plot_heatmap_step_ = partial(plot_heatmap_step, vis_dir=f"{args.save_dir}/vis_heatmap_optim", index=i, step=j)
                    plot_dgse = partial(plot_heatmap_step, vis_dir=f"{args.save_dir}/vis_dgse", index=i, step=j)
                    plot_vis_activemap_ = partial(plot_vis_activemap, vis_dir=f"{args.save_dir}/vis_activemap_optim", index=i)
                    plot_func_dict = {'plot_heatmap':plot_heatmap_step_, 'plot_activemap': plot_vis_activemap_, 'plot_dgse': plot_dgse}
                    self.dgse, last_loss = optim_energy(
                        depth0,
                        depth_map,
                        normal_map,
                        vertex_map,
                        Tlw_i, #w2c
                        self.dgv,
                        self.dgse,
                        self.dgw,
                        self._kdtree,
                        K,
                        self.knn,
                        plot_func_dict,
                    )
                    if last_loss < 1e-5:
                        break

                # update Tlw

            # fusion
            if i == 0:
                self.tsdf_volume.integrate(
                    depth0, K, Tlw, obs_weight=1.0, color_img=color0
                )
            else:
                # self.tsdf_volume.integrate(
                #     depth0, K, Tlw, obs_weight=1.0, color_img=color0
                # )
                self.tsdf_volume.integrate_dynamic(depth0,
                                    self.dgv,
                                    self.dgse,
                                    self.dgw,
                                    self._kdtree,
                                    K,
                                    Tlw,
                                    obs_weight=1.,
                                    color_img=color0)
            t1 = get_time()
            t += [t1 - t0]
            print("processed frame: {:d}, time taken: {:f}s".format(i, t1 - t0))
            poses += [Tlw.cpu().numpy()]
            poses_gt += [pose_gt.cpu().numpy()]
            if i == 0:
                self.construct_graph()
                print(f"Done, now we have {self.dgv.shape[0]} nodes!")
            else:
                print("Start update graph!")
                # try:
                self.update_graph(Tlw)
                print(f"Done, now we have {self.dgv.shape[0]} nodes!")
                # except Exception as e:
                #     print(f"Failed to update graph because of {e}! ")
            if i == 171:
                break
        avg_time = np.array(t).mean()
        print(
            "average processing time: {:f}s per frame, i.e. {:f} fps".format(
                avg_time, 1.0 / avg_time
            )
        )
        # compute tracking ATE
        self.poses_gt = np.stack(poses_gt, 0)
        self.poses = np.stack(poses, 0)
        traj_gt = np.array(poses_gt)[:, :3, 3]
        traj = np.array(poses)[:, :3, 3]
        rmse = np.sqrt(np.mean(np.linalg.norm(traj_gt - traj, axis=-1) ** 2))
        print("RMSE: {:f}".format(rmse))
        plt.plot(traj[:, 0], traj[:, 1])
        plt.plot(traj_gt[:, 0], traj_gt[:, 1])
        plt.legend(['Estimated', 'GT'])
        plt.savefig("Trajactoray.png")

    def save_mesh(self) -> None:
        """_summary_
        """        
        if not os.path.exists(self.args.save_dir):
            os.makedirs(self.args.save_dir)
        if args.fuse_color:
            verts, faces, norms, colors = self.tsdf_volume.get_mesh()
            partial_tsdf = trimesh.Trimesh(
                vertices=verts, faces=faces, vertex_normals=norms, vertex_colors=colors
            )
        else:
            verts, faces, norms = self.tsdf_volume.get_mesh()
            partial_tsdf = trimesh.Trimesh(
                vertices=verts, faces=faces, vertex_normals=norms
            )
        partial_tsdf.export(os.path.join(args.save_dir, "mesh.obj"))
        np.savez(os.path.join(args.save_dir, "traj.npz"), poses=self.poses)
        np.savez(os.path.join(args.save_dir, "traj_gt.npz"), poses=self.poses_gt)

    def construct_graph(self) -> None:
        """_summary_
        """        
        verts, faces, norms = self.tsdf_volume.get_mesh()
        self._radius = 0.1  # the paper said e = 0.01 in experiment
        self._vertices = verts
        self._faces = faces
        nodes_v, nodes_idx = uniform_sample(self._vertices, self._radius)
        # 获取nodes_v在x,y,z维度的最大最小值
        # x_min, x_max = nodes_v[:, 0].min(), nodes_v[:, 0].max()
        # # 平均值
        # x_mean = nodes_v[:, 0].mean()
        # y_min, y_max = nodes_v[:, 1].min(), nodes_v[:, 1].max()
        # y_mean = nodes_v[:, 1].mean()
        # z_min, z_max = nodes_v[:, 2].min(), nodes_v[:, 2].max()
        # z_mean = nodes_v[:, 2].mean()
        # print(f"nodes_v x_min: {x_min}, x_max: {x_max}")
        # print(f"nodes_v x_mean: {x_mean}")
        # print(f"nodes_v y_min: {y_min}, y_max: {y_max}")
        # print(f"nodes_v y_mean: {y_mean}")
        # print(f"nodes_v z_min: {z_min}, z_max: {z_max}")
        # print(f"nodes_v z_mean: {z_mean}")
        mesh = trimesh.Trimesh(vertices=verts,faces=faces)
        mesh.export(os.path.join(args.save_dir, f"init_mesh.obj"))
        point_cloud = trimesh.PointCloud(nodes_v)
        point_cloud.export(os.path.join(args.save_dir, f"node.ply"))
        self.dgv = torch.tensor(nodes_v.copy()).to(self.device)
        # define list of node warp
        dgse = []
        dgw = []
        for j in range(len(self.dgv)):
            dgse.append(
                torch.tensor([ 0.00, 0.00, 0.00, 0.00, 0.00, 0.00]).float()
            )
            dgw.append(torch.tensor(3.0 * self._radius))
        self.dgse = torch.stack(dgse).to(self.device)
        self.dgw = torch.stack(dgw).float().to(self.device)

        # construct kd tree
        self._kdtree = KDTree(nodes_v.astype(np.float32))

    def update_graph(self, Tlw: torch.tensor) -> None:
        """_summary_

        Args:
            Tlw (torch.tensor): _description_

        Returns:
            _type_: _description_
        """        
        verts_c, faces, norms = self.tsdf_volume.get_mesh()
        dists, idx = self._kdtree.query(verts_c, k=4, workers=-1)
        verts_c = torch.tensor(verts_c).to(self.device)
        verts_c_nn_idx = torch.tensor(idx).long()
        verts_c_nn = self.dgv[verts_c_nn_idx]
        verts_c_nn_dgw = self.dgw[verts_c_nn_idx]
        verts_c_rep = verts_c.view(-1, 1, 3).repeat_interleave(4, dim=1)
        verts_c_l2_dgw = (
            torch.linalg.norm(verts_c_nn - verts_c_rep, dim=2) / verts_c_nn_dgw
        )
        mask_unsupported = torch.min(verts_c_l2_dgw, dim=1).values >= 1

        def get_vec_helper(Xc, Tlw, dgv, dgse, dgw, node_to_nn):
            dgv_nn = dgv[node_to_nn]
            dgw_nn = dgw[node_to_nn]
            make_dq_from_vec_vmap = get_vmap_make_dq_from_vec()
            dgse_dq = make_dq_from_vec_vmap(dgse)
            dgse_nn = dgse_dq[node_to_nn]
            # assert dgse_nn.shape == (4, 8)
            # assert dgv_nn.shape == (4, 3)
            T = get_W(Xc, Tlw, dgse_nn, dgw_nn, dgv_nn)
            print("T:",T)
            re_dq = dqnorm(SE3_dq(T.view(4, 4)))

            # print("dq:",re_dq.view(8))
            vec = inverse_dq_to_vec(re_dq)
            return vec
        # 提取旋转向量 theta 和平移向量 t
        def extract_theta_from_q(q_real: torch.tensor) -> torch.tensor:
            """
            从旋转四元数 q_real 提取旋转向量 theta
            """
            
            # 计算旋转角度的模
            theta_norm = 2 * torch.acos(q_real[0])[None][0]  # 旋转角度的模
            
            # 避免 theta_norm == 0 的情况，用 where 代替 if 语句
            sin_half_theta = torch.sqrt(torch.clamp(1 - q_real[0]**2, min=1e-6))[None][0]  # 防止除以零
            axis = torch.stack([q_real[1], q_real[2], q_real[3]]) / sin_half_theta
            
            # 如果 theta_norm 为零，旋转向量应该为零向量
            theta = (theta_norm * axis)[None][0]
            theta = (torch.where(theta_norm == 0, torch.zeros(3)[None][0].to(q_real), theta))[None][0]
            
            return theta

        def extract_translation_from_dual(dq: torch.tensor) -> torch.tensor:
            
            # 根据对偶四元数和平移的关系推导平移向量 t
            translation = 2 * torch.tensor([
                -dq[4]*dq[1] + dq[5]*dq[0] - dq[6]*dq[3] + dq[7]*dq[2],
                -dq[4]*dq[2] + dq[5]*dq[3] + dq[6]*dq[0] - dq[7]*dq[1],
                -dq[4]*dq[3] - dq[5]*dq[2] + dq[6]*dq[1] + dq[7]*dq[0]
            ])[None][0]
            
            return translation

        get_vec_helper_vmap = vmap(
            get_vec_helper, in_dims=(0, None, None, None, None, 0)
        )
        nodes_v, nodes_idx = uniform_sample(
            verts_c[mask_unsupported].cpu(), self._radius
        )
        dists, idx = self._kdtree.query(nodes_v, k=4, workers=-1)
        nodes_v = torch.tensor(nodes_v).to(self.device)
        nodes_v_nn_idx = torch.tensor(idx).long().to(self.device)
        nodes_se = (
            get_vec_helper_vmap(
                nodes_v, Tlw, self.dgv, self.dgse, self.dgw, nodes_v_nn_idx
            )
            .float()
            .to(self.device)
        )
        assert nodes_se.shape[0] == nodes_v.shape[0]
        nodes_w = (
            torch.tensor(3.0 * self._radius)
            .view(1, 1)
            .repeat_interleave(nodes_v.shape[0], 0)
            .to(self.device)
            .view(-1)
        )

        self.dgv = torch.cat((self.dgv, nodes_v), dim=0).to(self.device)
        self.dgse = torch.cat((self.dgse, nodes_se), dim=0).to(self.device)
        self.dgw = torch.cat((self.dgw, nodes_w), dim=0).float().to(self.device)
        assert (
            self.dgv.shape[0] == self.dgw.shape[0]
            and self.dgv.shape[0] == self.dgse.shape[0]
        ), f"{self.dgv.shape, self.dgw.shape, self.dgse.shape, nodes_w.shape, nodes_v.shape}"

        # construct kd tree
        self._kdtree = KDTree(self.dgv.cpu())
        # pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # standard configs
    parser.add_argument(
        "--config",
        type=str,
        default="configs/fr1_desk.yaml",
        help="Path to config file.",
    )
    parser.add_argument(
        "--save_dir", type=str, default=None, help="Directory of saving results."
    )
    args = load_config(parser.parse_args())
    os.makedirs(args.save_dir, exist_ok=True)
    dynfu = DynFu(args)
    dynfu.process()
    if args.save_dir is not None:
        dynfu.save_mesh()
