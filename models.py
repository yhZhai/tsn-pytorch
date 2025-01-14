from torch import nn
from cost_volume import CostVolume, DisplacementMap
from ops.basic_ops import ConsensusModule, Identity
from transforms import *
import bninception
from torch.nn.init import normal_, constant_
import cost_volume_model

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class TSN(nn.Module):
    def __init__(self, num_class, num_segments, modality, base_model='resnet101', new_length=None, consensus_type='avg', before_softmax=True,
                 dropout=0.8, crop_num=1):
        super(TSN, self).__init__()
        self.modality = modality
        self.num_segments = num_segments
        self.reshape = True
        self.before_softmax = before_softmax
        self.dropout = dropout
        self.crop_num = crop_num
        self.consensus_type = consensus_type
        if self.modality == 'CV':
            self.cost_volume = CostVolume(2, 2)
        if not before_softmax and consensus_type != 'avg':
            raise ValueError("Only avg consensus can be used after Softmax")

        if new_length is None:
            if modality == 'RGB':
                self.new_length = 1
            elif modality == 'CV':
                self.new_length = 5
            else:
                self.new_length = 5
        else:
            self.new_length = new_length

        print(("""
Initializing TSN with base model: {}.
TSN Configurations:
    input_modality:     {}
    num_segments:       {}
    new_length:         {}
    consensus_module:   {}
    dropout_ratio:      {}
        """.format(base_model, self.modality, self.num_segments, self.new_length, consensus_type, self.dropout)))

        if self.modality == 'CV':
            self.input_size = 224
            self.input_mean = [104, 117, 128]
            self.input_std = [1]
            self._prepare_tsn(num_class, is_cv=True)
        else:
            self._prepare_base_model(base_model)
            self._prepare_tsn(num_class, is_cv=False)

        if self.modality == 'Flow':
            print("Converting the ImageNet model to a flow init model")
            self.base_model = self._construct_flow_model(self.base_model)
            print("Done. Flow model ready...")
        elif self.modality == 'RGBDiff':
            print("Converting the ImageNet model to RGB+Diff init model")
            self.base_model = self._construct_diff_model(self.base_model)
            print("Done. RGBDiff model ready.")
        elif self.modality == 'CV':
            # print("Converting the ImageNet model to CV init model")
            # self._construct_cv_model()
            self.prev_cv_model = cost_volume_model.PreModel()
            self.displacement_map = DisplacementMap(2, 2, tau=1)
            self.late_cv_model = cost_volume_model.LateModel()
            print("CV model ready.")

        self.consensus = ConsensusModule(consensus_type)

        if not self.before_softmax:
            self.softmax = nn.Softmax()

    def _prepare_tsn(self, num_class, is_cv):
        if not is_cv:
            feature_dim = getattr(self.base_model, self.base_model.last_layer_name).in_features
            if self.dropout == 0:
                setattr(self.base_model, self.base_model.last_layer_name, nn.Linear(feature_dim, num_class))
                self.new_fc = None
            else:
                setattr(self.base_model, self.base_model.last_layer_name, nn.Dropout(p=self.dropout))
                self.new_fc = nn.Linear(feature_dim, num_class)

            std = 0.001
            if self.new_fc is None:
                normal_(getattr(self.base_model, self.base_model.last_layer_name).weight, 0, std)
                constant_(getattr(self.base_model, self.base_model.last_layer_name).bias, 0)
            else:
                normal_(self.new_fc.weight, 0, std)
                constant_(self.new_fc.bias, 0)
            return feature_dim
        else:
            self.new_fc = nn.Sequential(nn.Dropout(p=self.dropout), nn.Linear(1024, num_class))
            return 1024

    def _prepare_base_model(self, base_model):
        if 'resnet' in base_model or 'vgg' in base_model:
            self.base_model = getattr(torchvision.models, base_model)(True)
            self.base_model.last_layer_name = 'fc'
            self.input_size = 224
            self.input_mean = [0.485, 0.456, 0.406]
            self.input_std = [0.229, 0.224, 0.225]

            if self.modality == 'Flow':
                self.input_mean = [0.5]
                self.input_std = [np.mean(self.input_std)]
            elif self.modality == 'RGBDiff':
                self.input_mean = [0.485, 0.456, 0.406] + [0] * 3 * self.new_length
                self.input_std = self.input_std + [np.mean(self.input_std) * 2] * 3 * self.new_length
        elif base_model == 'BNInception':
            self.base_model = bninception.bninception(101, pretrained=None)
            self.base_model.last_layer_name = 'fc'
            self.input_size = 224
            self.input_mean = [104, 117, 128]
            self.input_std = [1]

            if self.modality == 'Flow':
                self.input_mean = [128]
            elif self.modality == 'RGBDiff':
                self.input_mean = self.input_mean * (1 + self.new_length)

        elif 'inception' in base_model:
            import tf_model_zoo
            self.base_model = getattr(tf_model_zoo, base_model)()
            self.base_model.last_layer_name = 'classif'
            self.input_size = 299
            self.input_mean = [0.5]
            self.input_std = [0.5]
        else:
            raise ValueError('Unknown base model: {}'.format(base_model))

    def forward(self, input):
        b, _, h, w = input.shape
        # sample_len is the number of channels of a segment
        if self.modality == 'RGB':
            sample_len = 3 * self.new_length
        elif self.modality == 'CV':
            sample_len = 2 * self.new_length
        else:
            sample_len = 2 * self.new_length

        if self.modality == 'RGBDiff':
            sample_len = 3 * self.new_length
            input = self._get_diff(input)

        if self.modality == 'CV':
            input = input.view((-1, 3) + input.size()[-2:])
            input = self.prev_cv_model(input)
            input = nn.functional.interpolate(input, size=(224, 224), mode='bilinear')
            input = input.view((b * self.num_segments, -1) + input.size()[-2:])
            c = 64
            img1 = input[:, : input.shape[1] - c, :, :].contiguous()
            img2 = input[:, c:, :, :].contiguous()
            img1 = img1.view((-1, c) + input.size()[-2:])
            img2 = img2.view((-1, c) + input.size()[-2:])
            cost_volume = self.cost_volume(img1, img2)
            displacement_map = self.displacement_map(cost_volume)
            input = displacement_map.view((b, -1) + input.size()[-2:])
            base_out = self.late_cv_model(input.view((-1, sample_len) + input.size()[-2:]))
        else:
            base_out = self.base_model(input.view((-1, sample_len) + input.size()[-2:]))

        if self.dropout > 0:
            base_out = self.new_fc(base_out)

        if not self.before_softmax:
            base_out = self.softmax(base_out)
        if self.reshape:
            base_out = base_out.view((-1, self.num_segments) + base_out.size()[1:])

        output = self.consensus(base_out)
        return output.squeeze(1)

    def _get_diff(self, input, keep_rgb=False):
        input_c = 3 if self.modality in ["RGB", "RGBDiff"] else 2
        input_view = input.view((-1, self.num_segments, self.new_length + 1, input_c,) + input.size()[2:])
        if keep_rgb:
            new_data = input_view.clone()
        else:
            new_data = input_view[:, :, 1:, :, :, :].clone()

        for x in reversed(list(range(1, self.new_length + 1))):
            if keep_rgb:
                new_data[:, :, x, :, :, :] = input_view[:, :, x, :, :, :] - input_view[:, :, x - 1, :, :, :]
            else:
                new_data[:, :, x - 1, :, :, :] = input_view[:, :, x, :, :, :] - input_view[:, :, x - 1, :, :, :]

        return new_data

    def _construct_flow_model(self, base_model):
        # modify the convolution layers
        # Torch models are usually defined in a hierarchical way.
        # nn.modules.children() return all sub modules in a DFS manner
        modules = list(self.base_model.modules())
        first_conv_idx = list(filter(lambda x: isinstance(modules[x], nn.Conv2d), list(range(len(modules)))))[0]
        conv_layer = modules[first_conv_idx]
        container = modules[first_conv_idx - 1]

        # modify parameters, assume the first blob contains the convolution kernels
        params = [x.clone() for x in conv_layer.parameters()]
        kernel_size = params[0].size()
        new_kernel_size = kernel_size[:1] + (2 * self.new_length,) + kernel_size[2:]
        new_kernels = params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()

        new_conv = nn.Conv2d(2 * self.new_length, conv_layer.out_channels,
                             conv_layer.kernel_size, conv_layer.stride, conv_layer.padding,
                             bias=True if len(params) == 2 else False)
        new_conv.weight.data = new_kernels
        if len(params) == 2:
            new_conv.bias.data = params[1].data  # add bias if neccessary
        layer_name = list(container.state_dict().keys())[0][:-7]  # remove .weight suffix to get the layer name

        # replace the first convlution layer
        setattr(container, layer_name, new_conv)
        return base_model

    def _construct_diff_model(self, base_model, keep_rgb=False):
        # modify the convolution layers
        # Torch models are usually defined in a hierarchical way.
        # nn.modules.children() return all sub modules in a DFS manner
        modules = list(self.base_model.modules())
        first_conv_idx = list(filter(lambda x: isinstance(modules[x], nn.Conv2d), list(range(len(modules)))))[0]
        conv_layer = modules[first_conv_idx]
        container = modules[first_conv_idx - 1]

        # modify parameters, assume the first blob contains the convolution kernels
        params = [x.clone() for x in conv_layer.parameters()]
        kernel_size = params[0].size()
        if not keep_rgb:
            new_kernel_size = kernel_size[:1] + (3 * self.new_length,) + kernel_size[2:]
            new_kernels = params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()
        else:
            new_kernel_size = kernel_size[:1] + (3 * self.new_length,) + kernel_size[2:]
            new_kernels = torch.cat((params[0].data, params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()), 1)
            new_kernel_size = kernel_size[:1] + (3 + 3 * self.new_length,) + kernel_size[2:]

        new_conv = nn.Conv2d(new_kernel_size[1], conv_layer.out_channels,
                             conv_layer.kernel_size, conv_layer.stride, conv_layer.padding,
                             bias=True if len(params) == 2 else False)
        new_conv.weight.data = new_kernels
        if len(params) == 2:
            new_conv.bias.data = params[1].data  # add bias if neccessary
        layer_name = list(container.state_dict().keys())[0][:-7]  # remove .weight suffix to get the layer name

        # replace the first convolution layer
        setattr(container, layer_name, new_conv)
        return base_model

    # def load_state_dict(self, state_dict, strict=True):
    #     r"""Copies parameters and buffers from :attr:`state_dict` into
    #     this module and its descendants. If :attr:`strict` is ``True``, then
    #     the keys of :attr:`state_dict` must exactly match the keys returned
    #     by this module's :meth:`~torch.nn.Module.state_dict` function.
    #
    #     Arguments:
    #         state_dict (dict): a dict containing parameters and
    #             persistent buffers.
    #         strict (bool, optional): whether to strictly enforce that the keys
    #             in :attr:`state_dict` match the keys returned by this module's
    #             :meth:`~torch.nn.Module.state_dict` function. Default: ``True``
    #     """
    #     missing_keys = []
    #     unexpected_keys = []
    #     error_msgs = []
    #
    #     # copy state_dict so _load_from_state_dict can modify it
    #     metadata = getattr(state_dict, '_metadata', None)
    #     state_dict = state_dict.copy()
    #     if metadata is not None:
    #         state_dict._metadata = metadata
    #
    #     def load(module, prefix=''):
    #         local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
    #         module._load_from_state_dict(state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs)
    #         for name, child in module._modules.items():
    #             if child is not None:
    #                 load(child, prefix + name + '.')
    #
    #     load(self)
    #
    #     if strict:
    #         error_msg = ''
    #         if len(unexpected_keys) > 0:
    #             error_msgs.insert(
    #                 0, 'Unexpected key(s) in state_dict: {}. '.format(
    #                     ', '.join('"{}"'.format(k) for k in unexpected_keys)))
    #         if len(missing_keys) > 0:
    #             error_msgs.insert(
    #                 0, 'Missing key(s) in state_dict: {}. '.format(
    #                     ', '.join('"{}"'.format(k) for k in missing_keys)))
    #
    #     if len(error_msgs) > 0:
    #         raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(
    #             self.__class__.__name__, "\n\t".join(error_msgs)))

    @property
    def crop_size(self):
        return self.input_size

    @property
    def scale_size(self):
        return self.input_size * 256 // 224

    def get_augmentation(self):
        if self.modality == 'RGB':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75, .66]),
                                                   GroupRandomHorizontalFlip(is_flow=False)])
        elif self.modality == 'Flow':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75]),
                                                   GroupRandomHorizontalFlip(is_flow=True)])
        elif self.modality == 'RGBDiff':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75]),
                                                   GroupRandomHorizontalFlip(is_flow=False)])
        elif self.modality == 'CV':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75]),
                                                   GroupRandomHorizontalFlip(is_flow=False)])
