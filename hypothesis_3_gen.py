import os
import math
import torch
from PIL import Image
from tqdm import tqdm
from sklearn.decomposition import PCA
from model import Generator
from attack_methods import attack_initializer
import torchvision.transforms as T
import time
import numpy as np
import argparse

class watermark_optimization:
    def __init__(self, args):
        # Define hyper parameter
        self.device_ids = 0
        self.device = 'cuda:{}'.format(self.device_ids)
        self.ckpt = args.ckpt
        self.n_mean_latent = 10000  # num of style vector to sample
        self.img_size = args.img_size  # image size
        self.style_space_dim = 512
        self.key_len = args.key_len
        self.batch_size = args.batch_size
        self.mapping_network_layer = 8
        self.num_main_pc = self.style_space_dim - self.key_len  # 512 - 64, num of high var pc
        self.sd_moved = args.sd  # How many standard deviation to move
        self.lr = 0.2
        self.save_dir = args.save_dir
        self.relu = torch.nn.ReLU()
        self.log_size = int(math.log(self.img_size, 2))
        self.num_block = self.log_size * 2 - 2
        self.style_mixing = False
        # Get generator
        g_ema = Generator(self.img_size, self.style_space_dim, self.mapping_network_layer)
        g_ema.load_state_dict(torch.load(self.ckpt)["g_ema"], strict=False)  # load ckpt
        g_ema.eval()  # set to eval mode
        self.g_ema = g_ema.to(self.device)  # push to device

    def PCA(self):
        """Do PCA"""
        pca = PCA()
        print("Performing PCA...")
        if os.path.isfile('./PCA/pca.pt'):
            pca_dict = torch.load('./PCA/pca.pt')
            return pca_dict['sigma_64'], pca_dict['v_cap'], pca_dict['u_cap'], None, pca_dict['sigma_512'], pca_dict['latent_mean'], None
        else:
            with torch.no_grad():
                noise_sample = torch.randn(self.n_mean_latent, 512, device=self.device)  # get a bunch of Z
                latent_out = self.g_ema.style(noise_sample)  # get style vector from Z
                latent_out = latent_out.detach().cpu().numpy()
                pca.fit(latent_out)  # do pca for the style vector data distribution
                var = pca.explained_variance_  # get variance along each pc axis ranked from high to low
                pc = pca.components_  # get the pc ranked from high var to low var
                latent_mean = latent_out.mean(0)
                latent_std = sum(((latent_out - latent_mean) ** 2) / self.n_mean_latent) ** 0.5
        # Get V and U
        var_64 = torch.tensor(var[self.num_main_pc:512], dtype=torch.float32, device=self.device)  # [64,]
        var_64 = var_64.view(-1, 1)  # [64, 1]
        var_512 = torch.tensor(var, dtype=torch.float32, device=self.device)  # [64,]
        var_512 = var_512.view(-1, 1)  # [64, 1]
        sigma_64 = torch.sqrt(var_64)
        sigma_512 = torch.sqrt(var_512)
        v_cap = torch.tensor(pc[self.num_main_pc:512, :], dtype=torch.float32,
                             device=self.device)  # low var pc [64x512]
        u_cap = torch.tensor(pc[0:self.num_main_pc, :], dtype=torch.float32,
                             device=self.device)  # high var pc [448x512]
        pc = torch.tensor(pc, dtype=torch.float32,
                          device=self.device)  # full pc [512x512]

        latent_mean = torch.tensor(latent_mean, dtype=torch.float32,
                                   device=self.device)  # high var pc [1x512]
        self.latent_mean = latent_mean.view(-1, 1)
        latent_std = torch.tensor(latent_std, dtype=torch.float32,
                                  device=self.device)  # high var pc [1x512]
        latent_std = latent_std.view(-1, 1)
        print("PCA Done")
        return sigma_64, v_cap, u_cap, pc, sigma_512, self.latent_mean, latent_std

    def generate_with_alpha(self, alpha, u_cap_t, sigma_64, v_cap, noise):
        """
        I = G(wx,n)
        wx = (U^T)*alpha+c(v^T)sk
        v: last 64 pcs, [64,512]
        U: first 448 pcs, [448,512]
        c: number of standard deviation moved
        s: Diagonal matrix for last 64 pc's standard deviation
        k: 64 digit binary keys
        n: fixed noise
        """
        self.key = torch.randint(2, (self.key_len, self.batch_size), device=self.device)  # Get random key
        latent_out = torch.transpose(torch.matmul(u_cap_t, alpha)+self.latent_mean, 0, 1) #to check cosine similarity between alpha used for generating images and reconstructed alpha in classifier code.
        sk_real = torch.multiply(sigma_64, self.key) #considers only positive part.
        new_latent = latent_out + self.sd_moved * torch.matmul(torch.transpose(sk_real, 0, 1), v_cap)
        if self.style_mixing:
            imgs, _ = self.g_ema(
                [latent_out,new_latent], noise=noise, input_is_latent=True,inject_index=self.num_block-1)
        else:
            imgs, _ = self.g_ema(
                [new_latent], noise=noise, input_is_latent=True)

        imgs = imgs.detach()
        latent_out = latent_out.detach()
        new_latent = new_latent.detach()
        return imgs, latent_out, new_latent

    def make_image(self, tensor):
        """Image postprocessing for output"""
        return (
            tensor.detach()
                .clamp_(min=-1, max=1)
                .add(1)
                .div_(2)
                .mul(255)
                .round()
                .type(torch.uint8)
                .permute(0, 2, 3, 1)
                .to("cpu")
                .numpy()
        )

    def store_results(self, original_image_w0, original_image_wx,wx_before_augmentation, iter,shift):
        store_path_w0 = 'shift_{}/image_before_perturb/'.format(shift)
        store_path_wx = 'shift_{}/perturbed_image/'.format(shift)
        store_path_wa = 'shift_{}/image_before_attack/'.format(shift)
        isExist = os.path.exists(self.save_dir  + store_path_w0)
        if not isExist:
            os.makedirs(self.save_dir + store_path_w0)

        isExist = os.path.exists(self.save_dir + store_path_wx)
        if not isExist:
            os.makedirs(self.save_dir + store_path_wx)

        if args.augmentation != 'None':
            isExist = os.path.exists(self.save_dir + store_path_wa)
            if not isExist:
                os.makedirs(self.save_dir + store_path_wa)

        for i in range(self.batch_size):
            img_name = self.save_dir + store_path_w0 + "target_w0_{}.png".format(self.batch_size*iter + i)
            pil_img = Image.fromarray(original_image_w0[i])
            pil_img.save(img_name)
            img_name = self.save_dir + store_path_wx + "target_wx_{}.png".format(self.batch_size*iter + i)
            pil_img = Image.fromarray(original_image_wx[i])
            pil_img.save(img_name)

            if args.augmentation != 'None':
                img_name = self.save_dir + store_path_wa + "target_wa_{}.png".format(self.batch_size*iter + i)
                pil_img = Image.fromarray(wx_before_augmentation[i])
                pil_img.save(img_name)

    def generate_image(self, style_vector, noise):
        """generate image given style vector and noise"""
        style_vector = style_vector.view(self.batch_size, -1)
        img_generated, _ = self.g_ema(
            [style_vector], noise=noise, input_is_latent=True)
        return img_generated

    def get_noise(self):
        rng = np.random.default_rng(seed=2002)
        log_size = int(math.log(self.img_size, 2))

        noises = [torch.tensor(rng.standard_normal((1, 1, 2 ** 2, 2 ** 2)), dtype=torch.float32, device=self.device)]

        for i in range(3, log_size + 1):
            for _ in range(2):
                noises.append(torch.tensor(np.random.standard_normal((1, 1, 2 ** i, 2 ** i)), dtype=torch.float32,
                                           device=self.device))

        return noises
    def augmentation(self,image):
        return image
if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Image generator for generating perturbed images"
    )
    parser.add_argument(
        "--ckpt", type=str, default='./checkpoint/550000.pt', required=False, help="path to the model checkpoint"
    )

    parser.add_argument(
        "--img_size", type=int, default=256, help="output image sizes of the generator"
    )

    parser.add_argument(
        "--sample_size", type=int, default=10000, help="Number of sample generated"
    )

    parser.add_argument(
        "--sd", type=int, default=1, help="Standard deviation moved"
    )

    parser.add_argument(
        "--batch_size", type=int, default=12, help="Batch size for generating images"
    )

    parser.add_argument(
        "--key_len", type=int, default=64, help="Number of digit for the binary key"
    )

    parser.add_argument(
        "--save_dir", type=str, default='./test_images/', help="Directory for image saving"
    )

    parser.add_argument(
        "--augmentation", type=str, default='None', help="Augmentation method: Crop, Noise, Blur, Jpeg, Combination "
    )
    # parser.add_argument(
    #     "--shift", type=int, default=1, help="Augmentation method: Crop, Noise, Blur, Jpeg, Combination "
    # )
    start = time.time()  # count times to complete
    args = parser.parse_args()
    shifts = [1,64,128,192,256,320,384,448]
    # shift = args.shift
    fixed_sigma = 1
    args.save_dir = args.save_dir + "fixed_sigma_{}/".format(fixed_sigma).replace('.', '') # ToDO: DEL this part when publishing
    #args.save_dir = args.save_dir + "unfixed_sigma_1/"
    optim = watermark_optimization(args)
    for shift in shifts:
        sigma_64, _, _, pc, sigma_512, latent_mean, latent_std = optim.PCA()
        # fixed_sigma = sigma_512[shift:shift+64, :]
        sigma_64 = fixed_sigma * torch.ones_like(sigma_64)
        sigma_64 = sigma_64.repeat(1, optim.batch_size)

        v_cap = torch.tensor(pc[shift:shift+64, :], dtype=torch.float32,
                             device=optim.device)  # low var pc [64x512]
        u_cap = torch.cat([pc[0:shift, :],pc[shift+64:optim.style_space_dim, :]],dim=0)
        u_cap = torch.tensor(u_cap, dtype=torch.float32,
                             device=optim.device)  # high var pc [448x512]

        # Get projections of the latent mean(for initial guess)
        v_cap_t = torch.transpose(v_cap, 0, 1)
        ata = torch.inverse(torch.matmul(v_cap, torch.transpose(v_cap, 0, 1)))
        projection_v = torch.matmul(torch.matmul(torch.matmul(v_cap_t, ata), v_cap), latent_mean) #not used
        u_cap_t = torch.transpose(u_cap, 0, 1)
        ata = torch.inverse(torch.matmul(u_cap, torch.transpose(u_cap, 0, 1)))
        projection_u = torch.matmul(torch.matmul(torch.matmul(u_cap_t, ata), u_cap), latent_mean)
        sigma_448 = torch.cat([sigma_512[0:shift, :],sigma_512[shift+64:optim.style_space_dim, :]],dim=0)

        # Get the boundary of alpha
        alpha_bar = torch.zeros((512,1)).to(optim.device)  # solve for init of for alpha = [512x1] tensor
        max_alpha = alpha_bar + 3 * sigma_512
        min_alpha = alpha_bar - 3 * sigma_512

        max_alpha = torch.cat([max_alpha[0:shift, :],max_alpha[shift+64:optim.style_space_dim, :]],dim=0)
        min_alpha = torch.cat([min_alpha[0:shift, :],min_alpha[shift+64:optim.style_space_dim, :]],dim=0)

        noise = optim.get_noise()

        number_of_images = args.sample_size
        key = []
        wx = []
        w0 = []
        # Get batched
        alpha_bar = alpha_bar.repeat(1, optim.batch_size)
        sigma_448 = sigma_448.repeat(1, optim.batch_size)
        for iter in tqdm(range(int(number_of_images / optim.batch_size) + 1)):
            rand_alpha = torch.multiply(sigma_448, torch.randn((optim.num_main_pc, optim.batch_size),
                                                               device=optim.device))
            target_img, target_w0, target_wx = optim.generate_with_alpha(rand_alpha, u_cap_t, sigma_64, v_cap, noise)
            wx_before_augmentation = optim.make_image(target_img)
            original_image = optim.generate_image(target_w0, noise)
            target_img = optim.augmentation(target_img)
            w0_image = optim.make_image(original_image)
            wx_image = optim.make_image(target_img)
            for i in range(optim.batch_size):
                wx.append(target_wx[i])
                w0.append(target_w0[i])
                key.append(optim.key[:, i])
            optim.store_results(w0_image, wx_image,wx_before_augmentation, iter,shift)

        result_file = {
            "wx": wx,
            "w0": w0,
            "key": key,
        }
        torch.save(result_file, args.save_dir +'shift_{}/'.format(shift)+ 'test_data.pt')

        result_file = {
            "sigma_512": sigma_512,
            "sigma_64": sigma_64[:, 0].view(-1,1),
            "v_cap": v_cap,
            "u_cap": u_cap,
            "latent_mean": latent_mean,
        }
        torch.save(result_file, args.save_dir +'shift_{}/'.format(shift)+ 'pca.pt')

