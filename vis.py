import os
os.environ["CUDA_VISIBLE_DEVICES"] = '2/'
import cv2
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import transforms
from torchvision import models
import matplotlib.pyplot as plt

from timesformer.datasets import utils as utils
from timesformer.datasets import video_container as container
from timesformer.datasets import decoder as decoder
import av
import torch
from timesformer.models.vit import  MLPTest,TimeSformer

import torch.nn.functional as F


class ActivationsAndGradients:
    """ Class for extracting activations and
    registering gradients from targeted intermediate layers """

    def __init__(self, model, target_layers, reshape_transform):
        self.model = model
        self.gradients = []
        self.activations = []
        self.reshape_transform = reshape_transform
        self.handles = []
        for target_layer in target_layers:
            self.handles.append(
                target_layer.register_forward_hook(
                    self.save_activation))
            # Backward compatibility with older pytorch versions:
            if hasattr(target_layer, 'register_full_backward_hook'):
                self.handles.append(
                    target_layer.register_full_backward_hook(
                        self.save_gradient))
            else:
                self.handles.append(
                    target_layer.register_backward_hook(
                        self.save_gradient))

    def save_activation(self, module, input, output):
        activation = output
        if self.reshape_transform is not None:
            activation = self.reshape_transform(activation)
        self.activations.append(activation.cpu().detach())

    def save_gradient(self, module, grad_input, grad_output):
        # Gradients are computed in reverse order
        grad = grad_output[0]
        if self.reshape_transform is not None:
            grad = self.reshape_transform(grad)
        self.gradients = [grad.cpu().detach()] + self.gradients

    def __call__(self, x):
        self.gradients = []
        self.activations = []
        return self.model(x)

    def release(self):
        for handle in self.handles:
            handle.remove()


class GradCAM:
    def __init__(self, model, target_layers, reshape_transform=None):
        self.model = model.eval()
        self.target_layers = target_layers
        self.reshape_transform = reshape_transform
        self.activations_and_grads = ActivationsAndGradients(
            self.model, target_layers, reshape_transform)

    """ Get a vector of weights for every channel in the target layer.
        Methods that return weights channels,
        will typically need to only implement this function. """

    @staticmethod
    def get_cam_weights(grads):
        print(grads.shape)
        return np.mean(grads, axis=(1, 2), keepdims=True)

    @staticmethod
    def get_loss(output, target_category):
        loss = 0
        for i in range(len(target_category)):
            loss = loss + output[i, target_category[i]]
        return loss

    def get_cam_image(self, activations, grads):
        weights = self.get_cam_weights(grads)
        weighted_activations = weights * activations
        cam = weighted_activations.sum(axis=1)

        return cam

    @staticmethod
    def get_target_width_height(input_tensor):
        width, height = input_tensor.size(-1), input_tensor.size(-2)
        return width, height

    def compute_cam_per_layer(self, input_tensor):
        activations_list = [a.cpu().data.numpy()
                            for a in self.activations_and_grads.activations]
        grads_list = [g.cpu().data.numpy()
                      for g in self.activations_and_grads.gradients]
        target_size = self.get_target_width_height(input_tensor)

        cam_per_target_layer = []
        # Loop over the saliency image from every layer

        for layer_activations, layer_grads in zip(activations_list, grads_list):
            cam = self.get_cam_image(layer_activations, layer_grads)
            cam[cam < 0] = 0  # works like mute the min-max scale in the function of scale_cam_image
            scaled = self.scale_cam_image(cam, target_size)
            # print("a",layer_activations.shape)
            # print(layer_grads.shape)
            # print(cam.shape)
            # print(target_size.shape)
            # print(scaled.shape)

            cam_per_target_layer.append(scaled[:, None, :])

        return cam_per_target_layer

    def aggregate_multi_layers(self, cam_per_target_layer):
        cam_per_target_layer = np.concatenate(cam_per_target_layer, axis=1)
        cam_per_target_layer = np.maximum(cam_per_target_layer, 0)
        result = np.mean(cam_per_target_layer, axis=1)
        return self.scale_cam_image(result)

    @staticmethod
    def scale_cam_image(cam, target_size=None):
        result = []
        for img in cam:
            img = img - np.min(img)
            img = img / (1e-7 + np.max(img))
            if target_size is not None:
                img = cv2.resize(img, target_size)
            result.append(img)
        result = np.float32(result)

        return result

    def __call__(self, input_tensor, target_category=None):

        # 正向传播得到网络输出logits(未经过softmax)
        output = self.activations_and_grads(input_tensor)
        if isinstance(target_category, int):
            target_category = [target_category] * input_tensor.size(0)

        if target_category is None:
            target_category = np.argmax(output.cpu().data.numpy(), axis=-1)
            print(f"category id: {target_category}")
        else:
            assert (len(target_category) == input_tensor.size(0))

        self.model.zero_grad()
        loss = self.get_loss(output, target_category)
        loss.backward(retain_graph=True)

        # In most of the saliency attribution papers, the saliency is
        # computed with a single target layer.
        # Commonly it is the last convolutional layer.
        # Here we support passing a list with multiple target layers.
        # It will compute the saliency image for every image,
        # and then aggregate them (with a default mean aggregation).
        # This gives you more flexibility in case you just want to
        # use all conv layers for example, all Batchnorm layers,
        # or something else.
        cam_per_layer = self.compute_cam_per_layer(input_tensor)
        return self.aggregate_multi_layers(cam_per_layer)

    def __del__(self):
        self.activations_and_grads.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.activations_and_grads.release()
        if isinstance(exc_value, IndexError):
            # Handle IndexError here...
            print(
                f"An exception occurred in CAM with block: {exc_type}. Message: {exc_value}")
            return True


def show_cam_on_image(img: np.ndarray,
                      mask: np.ndarray,
                      use_rgb: bool = False,
                      colormap: int = cv2.COLORMAP_JET) -> np.ndarray:
    """ This function overlays the cam mask on the image as an heatmap.
    By default the heatmap is in BGR format.
    :param img: The base image in RGB or BGR format.
    :param mask: The cam mask.
    :param use_rgb: Whether to use an RGB or BGR heatmap, this should be set to True if 'img' is in RGB format.
    :param colormap: The OpenCV colormap to be used.
    :returns: The default image with the cam overlay.
    """

    heatmap = cv2.applyColorMap(np.uint8(255 * mask), colormap)
    print("heat",heatmap.shape)
    if use_rgb:
        heatmap = cv2.cvtColor(heatmap, cv2.COLOR_BGR2RGB)
    heatmap = np.float32(heatmap) / 255

    if np.max(img) > 1:
        raise Exception(
            "The input image should np.float32 in the range [0, 1]")

    cam = heatmap + img
    cam = cam / np.max(cam)
    return np.uint8(255 * cam)


def image_proprecess(img_path):
    img = Image.open(img_path)
    data_transforms = transforms.Compose([
        transforms.Resize((384, 384), interpolation=3),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
    data = data_transforms(img)
    data = torch.unsqueeze(data,0)
    img_resize = img.resize((224,224))
    return img_resize,data

def Init_Setting():
    # model = models.mobilenet_v3_large(pretrained=True)
    #model.load_state_dict(torch.load('model.pth')
    model = TimeSformer(img_size=224, num_classes=94, num_frames=8, attention_type='divided_space_time',
                        pretrained_model='result/timesformer_frame4_rate32/checkpoints/checkpoint_epoch_00020.pyth')
    model = model.eval()
    return model


if __name__ == "__main__":

    path = "img/phone_JL_06.mp4"

    container = av.open(path)

    temporal_sample_index = 0 # -1随机采样，其它均匀采样，0表示从第0帧开始，2表示从第2帧开始。最大30
    NUM_ENSEMBLE_VIEWS = 10
    min_scale = 256
    max_scale = 320
    crop_size = 224
    sampling_rate = 8

    MEAN = [0.45, 0.45, 0.45]
    # List of input frame channel dimensions.

    # The std value of the video raw pixels across the R G B channels.
    STD = [0.225, 0.225, 0.225]

    frames = decoder.decode(
                    container,
                    sampling_rate,
                    8,
                    temporal_sample_index,
                    NUM_ENSEMBLE_VIEWS,
                    None,
                    target_fps=30,
                    backend="pyav",
                    max_spatial_scale=min_scale,
                )

    # Perform color normalization.
    frames = utils.tensor_normalize(frames, MEAN, STD)
                
    # T H W C -> C T H W.
    frames = frames.permute(3, 0, 1, 2)

    # Perform data augmentation.

    frames = utils.spatial_sampling(
        frames,
        spatial_idx=0,
        min_scale=min_scale,
        max_scale=max_scale,
        crop_size=crop_size,
        random_horizontal_flip=False,
        inverse_uniform_sampling=False,
    )

    # print(frames)
    # print(frames.shape)  #8,3,224,224

    # for i in range(frames.size(0)):

    imgs_path = "img/Image6.jpg"
    model = Init_Setting()
    target_layers = [model.model.blocks[-1].mlp]
    img, data = image_proprecess(imgs_path)
    data = frames.unsqueeze(0)

    print("1",data.shape)

    cam = GradCAM(model=model, target_layers=target_layers)
    target_category = None

    # data = data.cuda()
    grayscale_cam = cam(input_tensor=data, target_category=target_category)
    print("2",grayscale_cam.shape)
    grayscale_cam = grayscale_cam[0, :]
    print("3",grayscale_cam.shape)
    visualization = show_cam_on_image(np.array(img) / 255.,
                                      grayscale_cam,
                                      use_rgb=True)
    plt.imshow(visualization)
    plt.xticks()
    plt.yticks()
    plt.axis('on')
    plt.savefig("gradcam_image.jpg")
