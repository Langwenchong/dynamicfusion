import imageio
import numpy as np
import os
from tqdm import tqdm

# 设置图片路径和输出视频路径
data_path = "dataset/rgbd_dataset_freiburg3_walking_xyz/processed"  # 替换为实际的图片文件夹路径
video_path = os.path.join(data_path, "input.mp4")  # 输出视频文件路径
fps = 24  # 设置帧率（frames per second）

# 获取图片的总数量
num_images = len(os.listdir(os.path.join(data_path,"rgb")))  # 替换为实际的图片数量，例如 len(os.listdir(data_path))

# 使用 imageio 打开一个视频写入器
writer = imageio.get_writer(video_path, fps=fps, codec='libx264')

# 遍历所有图像并将其写入视频
for i in tqdm(range(num_images)):
    # 构造图片文件名，假设文件名是格式化的，例如 0000.png, 0001.png, ...
    img_path = os.path.join(data_path, "rgb/{:04d}.png".format(i))
    
    # 读取图像
    rgb = imageio.imread(img_path).astype(np.uint8)  # 确保图像为 uint8 类型

    # 将图像帧写入视频
    writer.append_data(rgb)

# 关闭视频写入器
writer.close()
