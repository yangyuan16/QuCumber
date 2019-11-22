# Copyright 2019 PIQuIL - All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import numpy as np
import torch

from qucumber.utils import cplx
from qucumber.utils import training_statistics as ts


class PosGradsUtils:
    def __init__(self, nn_state):
        self.nn_state = nn_state

    def compute_numerical_kl(self, target_psi, vis, Z):
        KL = 0.0
        for i in range(len(vis)):
            KL += ((target_psi[i, 0])**2) * ((target_psi[i, 0])**2).log()
            KL -= ((target_psi[i, 0])**2) * (self.nn_state.probability(
                vis[i], Z)).log().item()
        return KL

    def compute_numerical_NLL(self, data, Z):
        NLL = 0
        batch_size = len(data)

        for i in range(batch_size):
            NLL -= self.nn_state.probability(
                data[i], Z).log().item() / float(batch_size)

        return NLL

    def algorithmic_gradKL(self, target_psi, vis, **kwargs):
        Z = self.nn_state.compute_normalization(vis)
        grad_KL = torch.zeros(
            self.nn_state.rbm_am.num_pars,
            dtype=torch.double,
            device=self.nn_state.device,
        )
        for i in range(len(vis)):
            grad_KL += ((target_psi[i, 0])**2) * self.nn_state.gradient(vis[i])
            grad_KL -= self.nn_state.probability(
                vis[i], Z) * self.nn_state.gradient(vis[i])
        return [grad_KL]

    def algorithmic_gradNLL(self, data, k, **kwargs):
        return self.nn_state.compute_batch_gradients(k, data, data)

    def numeric_gradKL(self, target_psi, param, vis, eps, **kwargs):
        num_gradKL = []
        for i in range(len(param)):
            param[i] += eps

            Z = self.nn_state.compute_normalization(vis)
            KL_p = self.compute_numerical_kl(target_psi, vis, Z)

            param[i] -= 2 * eps

            Z = self.nn_state.compute_normalization(vis)
            KL_m = self.compute_numerical_kl(target_psi, vis, Z)

            param[i] += eps

            num_gradKL.append((KL_p - KL_m) / (2 * eps))

        return torch.stack(num_gradKL).to(param)

    def numeric_gradNLL(self, param, data, vis, eps, **kwargs):
        num_gradNLL = []
        for i in range(len(param)):
            param[i] += eps

            Z = self.nn_state.compute_normalization(vis)
            NLL_p = self.compute_numerical_NLL(data, Z)

            param[i] -= 2 * eps

            Z = self.nn_state.compute_normalization(vis)
            NLL_m = self.compute_numerical_NLL(data, Z)

            param[i] += eps

            num_gradNLL.append((NLL_p - NLL_m) / (2 * eps))

        return torch.tensor(np.array(num_gradNLL),
                            dtype=torch.double).to(param)


class ComplexGradsUtils:
    def __init__(self, nn_state):
        self.nn_state = nn_state

    def load_target_psi(self, bases, psi_data):
        psi_dict = {}
        D = int(len(psi_data) / float(len(bases)))

        if isinstance(psi_data, torch.Tensor):
            psi_data = psi_data.clone().detach().to(dtype=torch.double)
        else:
            psi_data = torch.tensor(psi_data, dtype=torch.double)

        for b in range(len(bases)):
            psi = torch.zeros(2, D, dtype=torch.double)
            psi_real = psi_data[b * D:(b + 1) * D, 0]
            psi_imag = psi_data[b * D:(b + 1) * D, 1]
            psi[0] = psi_real
            psi[1] = psi_imag
            psi_dict[bases[b]] = psi

        return psi_dict

    def transform_bases(self, bases_data):
        bases = []
        for i in range(len(bases_data)):
            tmp = ""
            for j in range(len(bases_data[i])):
                if bases_data[i][j] != " ":
                    tmp += bases_data[i][j]
            bases.append(tmp)
        return bases

    def rotate_psi_full(self, basis, full_unitary_dict, psi):
        U = full_unitary_dict[basis]
        Upsi = cplx.matmul(U, psi)
        return Upsi

    def rotate_psi(self, basis, unitary_dict, vis):
        N = self.nn_state.num_visible
        v = torch.zeros(N, dtype=torch.double, device=self.nn_state.device)
        psi_r = torch.zeros(2,
                            1 << N,
                            dtype=torch.double,
                            device=self.nn_state.device)

        for x in range(1 << N):
            Upsi = torch.zeros(2,
                               dtype=torch.double,
                               device=self.nn_state.device)
            num_nontrivial_U = 0
            nontrivial_sites = []
            for j in range(N):
                if basis[j] != "Z":
                    num_nontrivial_U += 1
                    nontrivial_sites.append(j)
            sub_state = self.nn_state.generate_hilbert_space(num_nontrivial_U)

            for xp in range(1 << num_nontrivial_U):
                cnt = 0
                for j in range(N):
                    if basis[j] != "Z":
                        v[j] = sub_state[xp][cnt]
                        cnt += 1
                    else:
                        v[j] = vis[x, j]

                U = torch.tensor([1.0, 0.0],
                                 dtype=torch.double,
                                 device=self.nn_state.device)
                for ii in range(num_nontrivial_U):
                    tmp = unitary_dict[basis[nontrivial_sites[ii]]]
                    tmp = tmp[:,
                              int(vis[x][nontrivial_sites[ii]]),
                              int(v[nontrivial_sites[ii]]), ]
                    U = cplx.scalar_mult(U, tmp)

                Upsi += cplx.scalar_mult(U, self.nn_state.psi(v).squeeze())

            psi_r[:, x] = Upsi
        return psi_r

    def compute_numerical_NLL(self, data_samples, data_bases, Z, unitary_dict,
                              vis):
        NLL = 0
        batch_size = len(data_samples)
        b_flag = 0
        for i in range(batch_size):
            bitstate = []
            for j in range(self.nn_state.num_visible):
                ind = 0
                if data_bases[i][j] != "Z":
                    b_flag = 1
                bitstate.append(int(data_samples[i, j].item()))
            ind = int("".join(str(i) for i in bitstate), 2)
            if b_flag == 0:
                NLL -= (self.nn_state.probability(data_samples[i],
                                                  Z)).log().item() / batch_size
            else:
                psi_r = self.rotate_psi(data_bases[i], unitary_dict, vis)
                NLL -= (cplx.norm_sqr(psi_r[:, ind]).log() -
                        Z.log()).item() / batch_size
        return NLL

    def compute_numerical_kl(self, psi_dict, vis, Z, unitary_dict, bases):
        N = self.nn_state.num_visible
        psi_r = torch.zeros(2,
                            1 << N,
                            dtype=torch.double,
                            device=self.nn_state.device)
        KL = 0.0
        for i in range(len(vis)):
            KL += (cplx.norm_sqr(psi_dict[bases[0]][:, i]) *
                   cplx.norm_sqr(psi_dict[bases[0]][:, i]).log() /
                   float(len(bases)))
            KL -= (cplx.norm_sqr(psi_dict[bases[0]][:, i]) *
                   self.nn_state.probability(vis[i], Z).log().item() /
                   float(len(bases)))

        for b in range(1, len(bases)):
            psi_r = self.rotate_psi(bases[b], unitary_dict, vis)
            for ii in range(len(vis)):
                if cplx.norm_sqr(psi_dict[bases[b]][:, ii]) > 0.0:
                    KL += (cplx.norm_sqr(psi_dict[bases[b]][:, ii]) *
                           cplx.norm_sqr(psi_dict[bases[b]][:, ii]).log() /
                           float(len(bases)))

                KL -= (cplx.norm_sqr(psi_dict[bases[b]][:, ii]) *
                       cplx.norm_sqr(psi_r[:, ii]).log() / float(len(bases)))
                KL += (cplx.norm_sqr(psi_dict[bases[b]][:, ii]) * Z.log() /
                       float(len(bases)))

        return KL

    def algorithmic_gradNLL(self, data_samples, data_bases, k, **kwargs):
        return self.nn_state.compute_batch_gradients(k, data_samples,
                                                     data_samples, data_bases)

    def numeric_gradNLL(self, data_samples, data_bases, unitary_dict, param,
                        vis, eps, **kwargs):
        num_gradNLL = []
        for i in range(len(param)):
            param[i] += eps

            Z = self.nn_state.compute_normalization(vis)
            NLL_p = self.compute_numerical_NLL(data_samples, data_bases, Z,
                                               unitary_dict, vis)
            param[i] -= 2 * eps

            Z = self.nn_state.compute_normalization(vis)
            NLL_m = self.compute_numerical_NLL(data_samples, data_bases, Z,
                                               unitary_dict, vis)

            param[i] += eps

            num_gradNLL.append((NLL_p - NLL_m) / (2 * eps))

        return torch.tensor(np.array(num_gradNLL),
                            dtype=torch.double).to(param)

    def numeric_gradKL(self, param, psi_dict, vis, unitary_dict, bases, eps,
                       **kwargs):
        num_gradKL = []
        for i in range(len(param)):
            param[i] += eps

            Z = self.nn_state.compute_normalization(vis)
            KL_p = self.compute_numerical_kl(psi_dict, vis, Z, unitary_dict,
                                             bases)

            param[i] -= 2 * eps

            Z = self.nn_state.compute_normalization(vis)
            KL_m = self.compute_numerical_kl(psi_dict, vis, Z, unitary_dict,
                                             bases)
            param[i] += eps

            num_gradKL.append((KL_p - KL_m) / (2 * eps))

        return torch.stack(num_gradKL).to(param)

    def algorithmic_gradKL(self, psi_dict, vis, unitary_dict, bases, **kwargs):
        grad_KL = [
            torch.zeros(
                self.nn_state.rbm_am.num_pars,
                dtype=torch.double,
                device=self.nn_state.device,
            ),
            torch.zeros(
                self.nn_state.rbm_ph.num_pars,
                dtype=torch.double,
                device=self.nn_state.device,
            ),
        ]
        Z = self.nn_state.compute_normalization(vis).to(
            device=self.nn_state.device)

        for i in range(len(vis)):
            grad_KL[0] += (
                cplx.norm_sqr(psi_dict[bases[0]][:, i]) *
                self.nn_state.rbm_am.effective_energy_gradient(vis[i]) /
                float(len(bases)))
            grad_KL[0] -= (
                self.nn_state.probability(vis[i], Z) *
                self.nn_state.rbm_am.effective_energy_gradient(vis[i]) /
                float(len(bases)))

        for b in range(1, len(bases)):
            for i in range(len(vis)):
                rotated_grad = self.nn_state.gradient(bases[b], vis[i])
                grad_KL[0] += (cplx.norm_sqr(psi_dict[bases[b]][:, i]) *
                               rotated_grad[0] / float(len(bases)))
                grad_KL[1] += (cplx.norm_sqr(psi_dict[bases[b]][:, i]) *
                               rotated_grad[1] / float(len(bases)))
                grad_KL[0] -= (
                    self.nn_state.probability(vis[i], Z) *
                    self.nn_state.rbm_am.effective_energy_gradient(vis[i]) /
                    float(len(bases)))
        return grad_KL


class DensityGradsUtils:
    def __init__(self, nn_state):
        self.nn_state = nn_state

    def transform_bases(self, bases_data):
        bases = []
        for i in range(len(bases_data)):
            tmp = ""
            for j in range(len(bases_data[i])):
                if bases_data[i][j] != " ":
                    tmp += bases_data[i][j]
            bases.append(tmp)
        return bases

    # NOTE: Not sure if compute_numerical_NLL function for
    # density matrix is useful, below is only for pure states
    # def compute_numerical_NLL(self, data_samples, data_bases, Z, unitary_dict, vis):
    #     NLL = 0
    #     batch_size = len(data_samples)
    #     b_flag = 0
    #     for i in range(batch_size):
    #         bitstate = []
    #         for j in range(self.nn_state.num_visible):
    #             ind = 0
    #             if data_bases[i][j] != "Z":
    #                 b_flag = 1
    #             bitstate.append(int(data_samples[i, j].item()))
    #         ind = int("".join(str(i) for i in bitstate), 2)
    #         if b_flag == 0:
    #             NLL -= (
    #                 self.nn_state.probability(data_samples[i], Z)
    #             ).log().item() / batch_size
    #         else:
    #             psi_r = self.rotate_psi(data_bases[i], unitary_dict, vis)
    #             NLL -= (
    #                 cplx.norm_sqr(psi_r[:, ind]).log() - Z.log()
    #             ).item() / batch_size
    #     return NLL

    # NOTE: Not sure if the NLL will ever be used. Keeping these
    # here, commented out, for possible future reference

    # def algorithmic_gradNLL(self, data_samples, data_bases, k, **kwargs):
    #     return self.nn_state.compute_batch_gradients(
    #         k, data_samples, data_samples, data_bases
    #     )

    # def numeric_gradNLL(
    #     self, data_samples, data_bases, unitary_dict, param, vis, eps, **kwargs
    # ):
    #     num_gradNLL = []
    #     for i in range(len(param)):
    #         param[i] += eps

    #         Z = self.nn_state.compute_normalization(vis)
    #         NLL_p = self.compute_numerical_NLL(
    #             data_samples, data_bases, Z, unitary_dict, vis
    #         )
    #         param[i] -= 2 * eps

    #         Z = self.nn_state.compute_normalization(vis)
    #         NLL_m = self.compute_numerical_NLL(
    #             data_samples, data_bases, Z, unitary_dict, vis
    #         )

    #         param[i] += eps

    #         num_gradNLL.append((NLL_p - NLL_m) / (2 * eps))

    #     return torch.tensor(np.array(num_gradNLL), dtype=torch.double).to(param)

    def compute_numerical_KL(self, target, bases, v_space, a_space):
        return ts.density_matrix_KL(self.nn_state, target, bases, v_space,
                                    a_space)

    def algorithmic_gradKL(self, data_samples, data_bases, v_space, **kwargs):
        grad = [0.0, 0.0]

        grad_data = [
            torch.zeros(2,
                        getattr(self.nn_state, net).num_pars,
                        dtype=torch.double) for net in self.nn_state.networks
        ]

        grad_model = [
            torch.zeros(self.nn_state.rbm_am.num_pars, dtype=torch.double)
        ]

        rho_rbm = self.nn_state.rhoRBM(v_space, v_space)

        for i in range(data_samples.shape[0]):
            data_gradient = self.nn_state.gradient(data_bases[i],
                                                   data_samples[i])
            grad_data[0] += data_gradient[0]
            grad_data[1] += data_gradient[1]

        # Can just take the real parts since the imaginary parts will have cancelled out
        grad[0] = -cplx.real(grad_data[0]) / float(data_samples.shape[0])
        grad[1] = -cplx.real(grad_data[1]) / float(data_samples.shape[0])

        for i in range(2**self.nn_state.num_visible):
            grad_model[0] += rho_rbm[0][i][i] * cplx.real(
                self.nn_state.am_grads(v_space[i], v_space[i]))

        grad[0] += grad_model[0]

        return grad

    def numeric_gradKL(self, param, target, v_space, a_space, bases, eps,
                       **kwargs):
        num_gradKL = []
        param_shape = param.shape
        param = param.view(-1)

        for i in range(len(param)):
            param[i] += eps
            KL_p = self.compute_numerical_KL(target, bases, v_space, a_space)

            param[i] -= 2 * eps
            KL_m = self.compute_numerical_KL(target, bases, v_space, a_space)

            param[i] += eps
            num_gradKL.append((KL_p - KL_m) / (2 * eps))

        param = param.reshape(param_shape)
        return torch.tensor(num_gradKL, dtype=torch.double)

    def all_num_grads(self, target, v_space, a_space, bases, eps):
        num_grads = []
        for _n, net in enumerate(self.nn_state.networks):
            rbm = getattr(self.nn_state, net)
            num_grad = torch.tensor([]).to(dtype=torch.double)
            counter = 0
            for param in rbm.parameters():
                num_grad = torch.cat((
                    num_grad,
                    self.numeric_gradKL(param, target, v_space, a_space, bases,
                                        eps),
                ))
                counter += 1
            num_grads.append(num_grad)

        return num_grads

    def test_grads(self, alg_grads, alg_grads_nogibbs, num_grads):
        for n, net in enumerate(self.nn_state.networks):
            print("\nRBM: %s" % net)
            rbm = getattr(self.nn_state, net)

            param_ranges = {}
            counter = 0
            for param_name, param in rbm.named_parameters():
                param_ranges[param_name] = range(counter,
                                                 counter + param.numel())
                counter += param.numel()

            for i, grad in enumerate(num_grads[n]):
                p_name, at_start = self.nn_state.get_param_status(
                    i, param_ranges)
                if at_start:
                    print(f"\nTesting {p_name}...")
                    print(f"Numerical KL\tAlg KL\t\tAlg KL No Gibbs")

                print("{: 10.8f}\t{: 10.8f}\t{: 10.8f}\t\t".format(
                    grad, alg_grads[n][i].item(),
                    alg_grads_nogibbs[n][i].item()))
