from pathlib import Path
import open3d as o3d
from scene.cameras import Camera
import torch
from scene.gaussian_model import GaussianModel
from gaussian_renderer import render
from utils.graphic_utils import depth_to_points
from utils.config_utils import Configuration
from utils.logging_utils import get_logger
from utils.sampling_utils import sample_uniform
from omegaconf import OmegaConf
from dataclasses import dataclass
import numpy as np
from scene.frame import Frame
from slam.local_model import LocalModel
from rich.progress import Progress, SpinnerColumn, TextColumn

logger = get_logger("postprocessing")


@dataclass
class ResultModel:
    id: int
    world_T_model: list[float]
    filename: str
    frame_ids: list[int]


@dataclass
class ResultFrame:
    id: int
    timestamp: float
    model_T_frame: list[float]
    projmatrix: list[float]
    model_id: int


@dataclass
class ResultGraph:
    models: list[ResultModel]
    frames: list[ResultFrame]

    def __str__(self):
        return f"ResultGraph with {len(self.models)} models " \
            f"and {len(self.frames)} frames."

    @staticmethod
    def from_slam(cfg: Configuration,
                  models: list[LocalModel],
                  output_dir: Path) -> "ResultGraph":
        frame_id = 0
        model_lst = []
        frame_lst = []
        for mid, model in enumerate(models):
            # Save pose in kitti format for simplicity
            wTm = model.world_T_model.cpu().numpy()[:3].reshape(-1)
            filename = output_dir / f"{mid:04d}.ply"
            frame_ids = []
            for frame in model.keyframes:
                timestamp = frame.timestamp
                mTf = frame.model_T_frame.cpu().numpy()[:3].reshape(-1)
                # Save only fx, fy, cx, cy
                projmatrix = frame.camera.projection_matrix.cpu().numpy()
                projmatrix = projmatrix.T
                projmatrix = [projmatrix[0, 0].item(), projmatrix[1, 1].item(),
                              projmatrix[0, 2].item(), projmatrix[1, 2].item()]
                rframe = ResultFrame(
                    id=frame_id,
                    timestamp=timestamp,
                    model_T_frame=mTf.tolist(),
                    projmatrix=projmatrix,
                    model_id=mid
                )
                frame_lst.append(rframe)
                frame_ids.append(frame_id)
                frame_id += 1
            rmodel = ResultModel(
                id=mid,
                filename=filename,
                world_T_model=wTm.tolist(),
                frame_ids=frame_ids
            )
            model_lst.append(rmodel)
        return ResultGraph(model_lst, frame_lst)

    @staticmethod
    def from_yaml(filename: Path) -> "ResultGraph":
        base_graph = OmegaConf.structured(ResultGraph)
        graph = OmegaConf.load(filename)
        return OmegaConf.merge(base_graph, graph)


@torch.no_grad()
def mesh_poisson(graph: ResultGraph,
                 cfg: Configuration,
                 graph_directory: Path,
                 kf_interval: int | None,
                 kf_samples: int | None,
                 min_opacity: float,
                 poisson_depth: int | None,
                 poisson_width: float | None,
                 poisson_min_density: float | None,
                 max_depth_dist: float,
                 use_median_depth: bool):
    """
    This method does the following:
    1) Iterate over frames and corresponding models
        (kf_interval to skip some frames)
    2) for each, instantiate model and camera and rasterize
    3) each image is filtered (min_opacity and max_depth_dist)
    3) each image is sampled and back-projected to PointCloud
    4) point-clouds are transformed into global frame and merged
    5) Poisson Reconstruction is ran over the global PointCloud
    """
    # Important notice:
    # Each frame should be rasterized in model frame.
    # world_T_model should be used only to move the cameras in world frame.
    # import rerun as rr
    # import time
    # rr.init("meshing")
    # rr.serve_grpc()
    global_pcd = o3d.geometry.PointCloud()
    processed_frames = 0
    with Progress() as progress:
        tbar = progress.add_task("Processing frames", total=len(graph.frames))
        for rmodel in graph.models:
            # Load the Gaussian model
            gmodel = GaussianModel()
            gmodel_filename = graph_directory / rmodel.filename
            logger.debug(f"Loading GaussianModel at {gmodel_filename}")
            gmodel.load_ply(graph_directory / rmodel.filename)
            # convert to numpy
            world_T_model = np.array(rmodel.world_T_model).reshape(3, 4)
            world_T_model = np.vstack([world_T_model, np.array([0, 0, 0, 1])])
            for rfid in rmodel.frame_ids:
                processed_frames += 1
                if kf_interval is not None and kf_interval > 0 and \
                        (processed_frames % kf_interval):
                    progress.update(tbar, advance=1)
                    continue
                rframe = graph.frames[rfid]
                assert rframe.id == rfid
                assert rmodel.id == rframe.model_id
                model_T_frame = np.array(rframe.model_T_frame).reshape(3, 4)
                model_T_frame = np.vstack(
                    [model_T_frame, np.array([0, 0, 0, 1])])

                camera_K = np.array([
                    [rframe.projmatrix[0], 0, rframe.projmatrix[2]],
                    [0, rframe.projmatrix[1], rframe.projmatrix[3]],
                    [0, 0, 1]
                ])
                width, height = cfg.preprocessing.image_width, \
                    cfg.preprocessing.image_height
                camera = Camera(K=camera_K,
                                image_depth=np.zeros((1, height, width)),
                                image_normal=np.zeros((3, height, width)),
                                image_valid=np.zeros((1, height, width)),
                                world_T_lidar=model_T_frame,
                                data_device=cfg.device)
                depth_ratio = 1.0 if use_median_depth else 0.0
                render_pkg = render(camera, gmodel, depth_ratio)

                depth = render_pkg["surf_depth"]
                normals = render_pkg["rend_normal"]
                alpha = render_pkg["rend_alpha"]
                dist = render_pkg["rend_dist"]

                invalid_mask = alpha < min_opacity
                invalid_mask = invalid_mask | (dist > max_depth_dist)
                invalid_mask = invalid_mask[0]

                depth[..., invalid_mask] = 0.0
                normals[..., invalid_mask] = 0.0

                xyz = depth_to_points(camera, depth, True)[..., ~invalid_mask]
                nxyz = normals[..., ~invalid_mask]
                xyz = xyz.T.cpu().numpy()
                nxyz = nxyz.T.cpu().numpy()
                sampled_idxs = np.random.choice(xyz.shape[0], kf_samples)
                xyz = np.copy(xyz[sampled_idxs])
                nxyz = np.copy(nxyz[sampled_idxs])

                pcd = o3d.geometry.PointCloud()
                pcd.points = o3d.utility.Vector3dVector(xyz)
                pcd.normals = o3d.utility.Vector3dVector(nxyz)
                pcd.transform(world_T_model)
                global_pcd += pcd
                progress.update(tbar, advance=1)
    logger.info(f"Merged{global_pcd}")
    logger.debug("Removing statistical outliers")
    global_pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    logger.info(
        "Running Poisson Surface Reconstruction. "
        "[green]This may take a while...[/green]")
    if poisson_depth is None or poisson_depth < 0 and \
            poisson_width > 0:
        logger.info(f"Using width={poisson_width}")
        mesh, densities = \
            o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
                global_pcd, width=poisson_width)
    else:
        logger.info(f"Using depth={poisson_depth}")
        mesh, densities = \
            o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
                global_pcd, depth=poisson_depth)

    if poisson_min_density > 0:
        logger.info(f"Removing vertices with "
                    f"density < {poisson_min_density} percentile")
        densities = np.asarray(densities)
        vertices_to_remove = densities < np.quantile(densities,
                                                     poisson_min_density)
        mesh.remove_vertices_by_mask(vertices_to_remove)
    mesh.compute_vertex_normals()
    return mesh
