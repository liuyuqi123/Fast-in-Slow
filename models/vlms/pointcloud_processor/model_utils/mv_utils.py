import numpy as np
import torch
import yaml
from easydict import EasyDict

LENGTH = 77
TRANS = -1.6

RESOLUTION = 224
TRANS = -1.6


def euler2mat(angle):
    """Convert euler angles to rotation matrix.
     :param angle: [3] or [b, 3]
     :return
        rotmat: [3] or [b, 3, 3]
    source
    https://github.com/ClementPinard/SfmLearner-Pytorch/blob/master/inverse_warp.py
    """
    if len(angle.size()) == 1:
        x, y, z = angle[0], angle[1], angle[2]
        _dim = 0
        _view = [3, 3]
    elif len(angle.size()) == 2:
        b, _ = angle.size()
        x, y, z = angle[:, 0], angle[:, 1], angle[:, 2]
        _dim = 1
        _view = [b, 3, 3]

    else:
        assert False

    cosz = torch.cos(z)
    sinz = torch.sin(z)

    zero = z.detach() * 0
    one = zero.detach() + 1
    zmat = torch.stack(
        [cosz, -sinz, zero, sinz, cosz, zero, zero, zero, one], dim=_dim
    ).reshape(_view)

    cosy = torch.cos(y)
    siny = torch.sin(y)

    ymat = torch.stack(
        [cosy, zero, siny, zero, one, zero, -siny, zero, cosy], dim=_dim
    ).reshape(_view)

    cosx = torch.cos(x)
    sinx = torch.sin(x)

    xmat = torch.stack(
        [one, zero, zero, zero, cosx, -sinx, zero, sinx, cosx], dim=_dim
    ).reshape(_view)

    rot_mat = xmat @ ymat @ zmat
    return rot_mat


def distribute(depth, _x, _y, size_x, size_y, image_height, image_width):
    """
    Distributes the depth associated with each point to the discrete coordinates (image_height, image_width) in a region
    of size (size_x, size_y).
    :param depth:
    :param _x:
    :param _y:
    :param size_x:
    :param size_y:
    :param image_height:
    :param image_width:
    :return:
    """

    assert size_x % 2 == 0 or size_x == 1
    assert size_y % 2 == 0 or size_y == 1
    batch, _ = depth.size()
    epsilon = torch.tensor([1e-12], requires_grad=False, device=depth.device)
    _i = torch.linspace(
        -size_x / 2, (size_x / 2) - 1, size_x, requires_grad=False, device=depth.device
    )
    _j = torch.linspace(
        -size_y / 2, (size_y / 2) - 1, size_y, requires_grad=False, device=depth.device
    )
    extended_x = (
        _x.unsqueeze(2).repeat([1, 1, size_x]) + _i
    ) 
    extended_y = (
        _y.unsqueeze(2).repeat([1, 1, size_y]) + _j
    )  

    extended_x = extended_x.unsqueeze(3).repeat(
        [1, 1, 1, size_y]
    )  
    extended_y = extended_y.unsqueeze(2).repeat(
        [1, 1, size_x, 1]
    ) 

    extended_x.ceil_()
    extended_y.ceil_()

    value = (
        depth.unsqueeze(2).unsqueeze(3).repeat([1, 1, size_x, size_y])
    ) 

    # all points that will be finally used
    masked_points = (
        (extended_x >= 0)
        * (extended_x <= image_height - 1)
        * (extended_y >= 0)
        * (extended_y <= image_width - 1)
        * (value >= 0)
    )

    true_extended_x = extended_x
    true_extended_y = extended_y

    # to prevent error
    extended_x = extended_x % image_height
    extended_y = extended_y % image_width

    distance = torch.abs(
        (extended_x - _x.unsqueeze(2).unsqueeze(3))
        * (extended_y - _y.unsqueeze(2).unsqueeze(3))
    )
    weight = masked_points.float() * (
        1 / (value + epsilon)
    ) 
    weighted_value = value * weight

    weight = weight.view([batch, -1])
    weighted_value = weighted_value.view([batch, -1])

    coordinates = (extended_x.view([batch, -1]) * image_width) + extended_y.view(
        [batch, -1]
    )
    coord_max = image_height * image_width
    true_coordinates = (
        true_extended_x.view([batch, -1]) * image_width
    ) + true_extended_y.view([batch, -1])
    true_coordinates[~masked_points.view([batch, -1])] = coord_max
    weight_scattered = torch.zeros(
        [batch, image_width * image_height], device=depth.device
    ).scatter_add(1, coordinates.long(), weight)

    masked_zero_weight_scattered = weight_scattered == 0.0
    weight_scattered += masked_zero_weight_scattered.float()

    weighed_value_scattered = torch.zeros(
        [batch, image_width * image_height], device=depth.device
    ).scatter_add(1, coordinates.long(), weighted_value)

    return weighed_value_scattered, weight_scattered


def points2depth(points, image_height, image_width, size_x=4, size_y=4):
    """
    :param points: [B, num_points, 3]
    :param image_width:
    :param image_height:
    :param size_x:
    :param size_y:
    :return:
        depth_recovered: [B, image_width, image_height]
    """

    epsilon = torch.tensor([1e-12], requires_grad=False, device=points.device)
    coord_x = (points[:, :, 0] / (points[:, :, 2] + epsilon)) * (
        image_width / image_height
    )  
    coord_y = points[:, :, 1] / (points[:, :, 2] + epsilon)  

    batch, total_points, _ = points.size()
    depth = points[:, :, 2]  
    _x = ((coord_x + 1) * image_height) / 2
    _y = ((coord_y + 1) * image_width) / 2

    weighed_value_scattered, weight_scattered = distribute(
        depth=depth,
        _x=_x,
        _y=_y,
        size_x=size_x,
        size_y=size_y,
        image_height=image_height,
        image_width=image_width,
    )

    depth_recovered = (weighed_value_scattered / weight_scattered).view(
        [batch, image_height, image_width]
    )

    return depth_recovered


def points2grid(points, resolution=518, depth=8):
    """Quantize each point cloud to a 3D grid.
    Args:
        points (torch.tensor): of size [B, _, 3]
    Returns:
        grid (torch.tensor): of size [B * self.num_views, depth, resolution, resolution]
    """

    batch, pnum, _ = points.shape

    pmax, pmin = points.max(dim=1)[0], points.min(dim=1)[0]
    pcent = (pmax + pmin) / 2
    pcent = pcent[:, None, :]
    prange = (pmax - pmin).max(dim=-1)[0][:, None, None]
    points = (points - pcent) / prange * 2.0
    points[:, :, :2] = points[:, :, :2] * 0.8

    depth_bias = 0.2
    _x = (points[:, :, 0] + 1) / 2 * resolution
    _y = (points[:, :, 1] + 1) / 2 * resolution
    _z = ((points[:, :, 2] + 1) / 2 + depth_bias) / (1 + depth_bias) * (depth - 2)

    _x.ceil_()
    _y.ceil_()
    z_int = _z.ceil()

    _x = torch.clip(_x, 1, resolution - 2)
    _y = torch.clip(_y, 1, resolution - 2)
    _z = torch.clip(_z, 1, depth - 2)

    return _x, _y


def points2pos(points, length, size_x=4, size_y=4, args=None):
    """
    :param points: [B, num_points, 3]
    :param size_x:
    :param size_y:
    :return:
        depth_recovered: [B, image_width, image_height]
    """

    direction = torch.tensor(args.pos_cor, requires_grad=False, device=points.device)
    direction = direction / torch.norm(direction)

    center = torch.mean(points, dim=1)
    relative_positions = points - center.unsqueeze(1)

    projections = torch.matmul(relative_positions, direction)

    min_projections = (
        torch.min(projections, dim=1)[0].unsqueeze(1).repeat([1, projections.size(1)])
    )
    max_projections = (
        torch.max(projections, dim=1)[0].unsqueeze(1).repeat([1, projections.size(1)])
    )
    normalized_projections = (projections - min_projections) / (
        max_projections - min_projections
    )

    final_projections = normalized_projections * length

    _i = torch.linspace(
        -size_x / 2,
        (size_x / 2) - 1,
        size_x,
        requires_grad=False,
        device=final_projections.device,
    )
    final_projections = final_projections + _i 
    # to prevent error
    final_projections = final_projections % length

    return final_projections


def points2pos_2d(points, image_height, image_width, size_x=4, size_y=4, args=None):
    """
    :param points: [B, num_points, 3]
    :param image_width:
    :param image_height:
    :param size_x:
    :param size_y:
    :return:
        depth_recovered: [B, image_width, image_height]
    """
    epsilon = torch.tensor([1e-12], requires_grad=False, device=points.device)
    coord_x = (points[:, :, 0] / (points[:, :, 2] + epsilon)) * (
        image_width / image_height
    ) 
    coord_y = points[:, :, 1] / (points[:, :, 2] + epsilon)  
    batch, total_points, _ = points.size()
    depth = points[:, :, 2] 
    _x = ((coord_x + 1) * image_height) / 2
    _y = ((coord_y + 1) * image_width) / 2

    assert size_x % 2 == 0 or size_x == 1
    assert size_y % 2 == 0 or size_y == 1
    _i = torch.linspace(
        -size_x / 2, (size_x / 2) - 1, size_x, requires_grad=False, device=depth.device
    )
    _j = torch.linspace(
        -size_y / 2, (size_y / 2) - 1, size_y, requires_grad=False, device=depth.device
    )
    extended_x = (
        _x.unsqueeze(2).repeat([1, 1, size_x]) + _i
    )  
    extended_y = (
        _y.unsqueeze(2).repeat([1, 1, size_y]) + _j
    ) 

    extended_x = extended_x.unsqueeze(3).repeat(
        [1, 1, 1, size_y]
    ) 
    extended_y = extended_y.unsqueeze(2).repeat(
        [1, 1, size_x, 1]
    )  

    # to prevent error
    extended_x = extended_x % image_height
    extended_y = extended_y % image_width

    return extended_x.squeeze(), extended_y.squeeze()


# source: https://discuss.pytorch.org/t/batched-index-select/9115/6
def batched_index_select(inp, dim, index):
    """
    input: B x * x ... x *
    dim: 0 < scalar
    index: B x M
    """
    views = [inp.shape[0]] + [1 if i != dim else -1 for i in range(1, len(inp.shape))]
    expanse = list(inp.shape)
    expanse[0] = -1
    expanse[dim] = -1
    index = index.view(views).expand(expanse)
    return torch.gather(inp, dim, index)


def point_fea_img_fea(point_fea, point_coo, h, w):
    """
    each point_coo is of the form (x*w + h). points not in the canvas are removed
    :param point_fea: [batch_size, num_points, feat_size]
    :param point_coo: [batch_size, num_points]
    :return:
    """
    assert len(point_fea.shape) == 3
    assert len(point_coo.shape) == 2
    assert point_fea.shape[0:2] == point_coo.shape

    coo_max = ((h - 1) * w) + (w - 1)
    mask_point_coo = (point_coo >= 0) * (point_coo <= coo_max)
    point_coo *= mask_point_coo.float()
    point_fea *= mask_point_coo.float().unsqueeze(-1)

    bs, _, fs = point_fea.shape
    point_coo = point_coo.unsqueeze(2).repeat([1, 1, fs])
    img_fea = torch.zeros([bs, h * w, fs], device=point_fea.device).scatter_add(
        1, point_coo.long(), point_fea
    )

    return img_fea


def distribute_img_fea_points(img_fea, point_coord):
    """
    :param img_fea: [B, C, H, W]
    :param point_coord: [B, num_points], each coordinate  is a scalar value given by (x * W) + y
    :return
        point_fea: [B, num_points, C], for points with coordinates outside the image, we return 0
    """
    B, C, H, W = list(img_fea.size())
    img_fea = img_fea.permute(0, 2, 3, 1).view([B, H * W, C])

    coord_max = ((H - 1) * W) + (W - 1)
    mask_point_coord = (point_coord >= 0) * (point_coord <= coord_max)
    mask_point_coord = mask_point_coord.float()
    point_coord = mask_point_coord * point_coord
    point_fea = batched_index_select(inp=img_fea, dim=1, index=point_coord.long())
    point_fea = mask_point_coord.unsqueeze(-1) * point_fea
    return point_fea


class PCViews:
    """For creating images from PC based on the view information. Faster as the
    repeated operations are done only once whie initialization.
    """

    def __init__(self, **kwargs):

        self.num_views = kwargs.get("num_views", 6)
        _views = np.asarray(
            [
                [[0 * np.pi / 2, 0, np.pi / 2], [0, 0, TRANS]],
                [[1 * np.pi / 2, 0, np.pi / 2], [0, 0, TRANS]],
                [[2 * np.pi / 2, 0, np.pi / 2], [0, 0, TRANS]],
                [[3 * np.pi / 2, 0, np.pi / 2], [0, 0, TRANS]],
                [[0, -np.pi / 2, np.pi / 2], [0, 0, TRANS]],
                [[0, np.pi / 2, np.pi / 2], [0, 0, TRANS]],
            ]
        )
        _views = _views[0: self.num_views]

        angle = torch.tensor(_views[:, 0, :]).float().cuda()
        self.rot_mat = euler2mat(angle).transpose(1, 2)
        self.translation = torch.tensor(_views[:, 1, :]).float().cuda()
        self.translation = self.translation.unsqueeze(1)

    def get_pos(self, points, args):
        """Get image based on the prespecified specifications.

        Args:
            points (torch.tensor): of size [B, _, 3]
        Returns:
            img (torch.tensor): of size [B * self.num_views, RESOLUTION,
                RESOLUTION]
        """

        b, _, _ = points.shape
        v = self.translation.shape[0]

        _points = self.point_transform(
            points=torch.repeat_interleave(points, v, dim=0),
            rot_mat=self.rot_mat.repeat(b, 1, 1),
            translation=self.translation.repeat(b, 1, 1),
        )

        pos_x = points2pos(points=_points, length=LENGTH, size_x=1, size_y=1, args=args)

        return pos_x, _points, self.rot_mat, self.translation

    def get_pos_2d(self, points, args=None):
        """Get image based on the prespecified specifications.

        Args:
            points (torch.tensor): of size [B, _, 3]
        Returns:
            img (torch.tensor): of size [B * self.num_views, RESOLUTION,
                RESOLUTION]
        """

        b, _, _ = points.shape
        v = self.translation.shape[0]

        _points = self.point_transform(
            points=torch.repeat_interleave(points, v, dim=0),
            rot_mat=self.rot_mat.repeat(b, 1, 1),
            translation=self.translation.repeat(b, 1, 1),
        )
        pos_x, pos_y = points2pos_2d(
            points=_points,
            image_height=RESOLUTION, 
            image_width=RESOLUTION,
            size_x=1,
            size_y=1,
            args=args,
        )
        return (
            pos_x,
            pos_y,
            _points,
        )  

    @staticmethod
    def point_transform(points, rot_mat, translation):  
        """
        :param points: [batch, num_points, 3]
        :param rot_mat: [batch, 3]
        :param translation: [batch, 1, 3]
        :return:
        """
        rot_mat = rot_mat.to(points.device)
        translation = translation.to(points.device)
        points = torch.matmul(points, rot_mat)
        points = points - translation
        return points


def merge_new_config(config, new_config):
    for key, val in new_config.items():
        if not isinstance(val, dict):
            if key == "_base_":
                with open(new_config["_base_"], "r") as f:
                    try:
                        val = yaml.load(f, Loader=yaml.FullLoader)
                    except:
                        val = yaml.load(f)
                config[key] = EasyDict()
                merge_new_config(config[key], val)
            else:
                config[key] = val
                continue
        if key not in config:
            config[key] = EasyDict()
        merge_new_config(config[key], val)
    return config


def cfg_from_yaml_file(cfg_file):
    config = EasyDict()
    with open(cfg_file, "r") as f:
        new_config = yaml.load(f, Loader=yaml.FullLoader)
    merge_new_config(config=config, new_config=new_config)
    return config
