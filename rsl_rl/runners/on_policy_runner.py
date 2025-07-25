# Copyright (c) 2021-2025, ETH Zurich and NVIDIA CORPORATION
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import os
import gc
import statistics
import time
import torch
from collections import deque
from tqdm import tqdm

import rsl_rl
from rsl_rl.algorithms import PPO, Distillation
from rsl_rl.env import VecEnv
from rsl_rl.modules import (
    ActorCritic,
    ActorCriticRecurrent,
    EmpiricalNormalization,
    StudentTeacher,
    StudentTeacherRecurrent,
)
from rsl_rl.utils import store_code_state


class OnPolicyRunner:
    """On-policy runner for training and evaluation."""

    def __init__(self, env: VecEnv, train_cfg: dict, log_dir: str | None = None, device="cpu"):
        self.cfg = train_cfg
        self.alg_cfg = train_cfg["algorithm"]
        self.policy_cfg = train_cfg["policy"]
        self.device = device
        self.env = env
        self.debug = train_cfg['debug']

        # check if multi-gpu is enabled
        self._configure_multi_gpu()

        # resolve training type depending on the algorithm
        if self.alg_cfg["class_name"] == "PPO":
            self.training_type = "rl"
        elif self.alg_cfg["class_name"] == "Distillation":
            self.training_type = "distillation"
        else:
            raise ValueError(f"Training type not found for algorithm {self.alg_cfg['class_name']}.")

        # resolve dimensions of observations
        obs, extras = self.env.get_observations()
        num_obs = obs.shape[1]

        # resolve type of privileged observations
        if self.training_type == "rl":
            if "critic" in extras["observations"]:
                self.privileged_obs_type = "critic"  # actor-critic reinforcement learnig, e.g., PPO
            else:
                self.privileged_obs_type = None
        if self.training_type == "distillation":
            if "teacher" in extras["observations"]:
                self.privileged_obs_type = "teacher"  # policy distillation
            else:
                self.privileged_obs_type = None

        # resolve dimensions of privileged observations
        if self.privileged_obs_type is not None:
            num_privileged_obs = extras["observations"][self.privileged_obs_type].shape[1]
        else:
            num_privileged_obs = num_obs

        # evaluate the policy class
        policy_class = eval(self.policy_cfg.pop("class_name"))
        policy: ActorCritic | ActorCriticRecurrent | StudentTeacher | StudentTeacherRecurrent = policy_class(
            num_obs, num_privileged_obs, self.env.num_actions, **self.policy_cfg
        ).to(self.device)

        # resolve dimension of rnd gated state
        if "rnd_cfg" in self.alg_cfg and self.alg_cfg["rnd_cfg"] is not None:
            # check if rnd gated state is present
            rnd_state = extras["observations"].get("rnd_state")
            if rnd_state is None:
                raise ValueError("Observations for the key 'rnd_state' not found in infos['observations'].")
            # get dimension of rnd gated state
            num_rnd_state = rnd_state.shape[1]
            # add rnd gated state to config
            self.alg_cfg["rnd_cfg"]["num_states"] = num_rnd_state
            # scale down the rnd weight with timestep (similar to how rewards are scaled down in legged_gym envs)
            self.alg_cfg["rnd_cfg"]["weight"] *= env.unwrapped.step_dt

        # if using symmetry then pass the environment config object
        if "symmetry_cfg" in self.alg_cfg and self.alg_cfg["symmetry_cfg"] is not None:
            # this is used by the symmetry function for handling different observation terms
            self.alg_cfg["symmetry_cfg"]["_env"] = env

        # initialize algorithm
        alg_class = eval(self.alg_cfg.pop("class_name"))
        self.alg: PPO | Distillation = alg_class(
            policy, device=self.device, **self.alg_cfg, multi_gpu_cfg=self.multi_gpu_cfg
        )

        # store training configuration
        self.upload_video = self.cfg["upload_video"]
        if self.upload_video:
            self.video_log_dir = os.path.join(log_dir, "videos", "train")
            self.uploaded_videos = os.listdir(self.video_log_dir)
        self.num_eval_envs = self.cfg["num_eval_envs"]
        self.eval_env_ids = torch.arange(self.num_eval_envs).to(self.device)
        self.num_steps_per_env = self.cfg["num_steps_per_env"]
        self.save_interval = self.cfg["save_interval"]
        self.empirical_normalization = self.cfg["empirical_normalization"]
        self.value_normalization = self.alg_cfg['value_normalization']
        if self.empirical_normalization:
            self.obs_normalizer = EmpiricalNormalization(shape=[num_obs], until=1.0e8).to(self.device)
            self.privileged_obs_normalizer = EmpiricalNormalization(shape=[num_privileged_obs], until=1.0e8).to(
                self.device
            )
        else:
            self.obs_normalizer = torch.nn.Identity().to(self.device)  # no normalization
            self.privileged_obs_normalizer = torch.nn.Identity().to(self.device)  # no normalization

        # init storage and model
        self.alg.init_storage(
            self.training_type,
            self.env.num_envs,
            self.num_steps_per_env,
            [num_obs],
            [num_privileged_obs],
            [self.env.num_actions],
        )

        # Decide whether to disable logging
        # We only log from the process with rank 0 (main process)
        self.disable_logs = self.is_distributed and self.gpu_global_rank != 0
        # Logging
        self.log_dir = log_dir
        self.writer = None
        self.tot_timesteps = 0
        self.tot_time = 0
        self.current_learning_iteration = 0
        self.git_status_repos = [rsl_rl.__file__]
        
        # @NOTE a little hack
        # in sweep mode, we need the torch.no_grad() as context
        self.sweep = self.cfg['sweep']

    def learn(self, num_learning_iterations: int, init_at_random_ep_len: bool = False, trial=None):  # noqa: C901
        # initialize writer
        if self.log_dir is not None and self.writer is None and not self.disable_logs:
            # Launch either Tensorboard or Neptune & Tensorboard summary writer(s), default: Tensorboard.
            self.logger_type = self.cfg.get("logger", "tensorboard")
            self.logger_type = self.logger_type.lower()

            if self.logger_type == "neptune":
                from rsl_rl.utils.neptune_utils import NeptuneSummaryWriter

                self.writer = NeptuneSummaryWriter(log_dir=self.log_dir, flush_secs=10, cfg=self.cfg)
                self.writer.log_config(self.env.cfg, self.cfg, self.alg_cfg, self.policy_cfg)
            elif self.logger_type == "wandb":
                from rsl_rl.utils.wandb_utils import WandbSummaryWriter

                self.writer = WandbSummaryWriter(log_dir=self.log_dir, flush_secs=10, cfg=self.cfg)
                self.writer.log_config(self.env.cfg, self.cfg, self.alg_cfg, self.policy_cfg)
            elif self.logger_type == "tensorboard":
                from torch.utils.tensorboard import SummaryWriter

                self.writer = SummaryWriter(log_dir=self.log_dir, flush_secs=10)
            else:
                raise ValueError("Logger type not found. Please choose 'neptune', 'wandb' or 'tensorboard'.")

        # check if teacher is loaded
        if self.training_type == "distillation" and not self.alg.policy.loaded_teacher:
            raise ValueError("Teacher model parameters not loaded. Please load a teacher model to distill.")

        # randomize initial episode lengths (for exploration)
        if init_at_random_ep_len:
            self.env.episode_length_buf = torch.randint_like(
                self.env.episode_length_buf, high=int(self.env.max_episode_length)
            )

        # start learning
        obs, extras = self.env.get_observations()
        privileged_obs = extras["observations"].get(self.privileged_obs_type, obs)
        obs, privileged_obs = obs.to(self.device), privileged_obs.to(self.device)
        self.train_mode()  # switch to train mode (for dropout for example)

        # Book keeping
        ep_infos = []
        rewbuffer = deque(maxlen=100)
        lenbuffer = deque(maxlen=100)
        csbuffer = deque(maxlen=100)
        cur_reward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
        cur_episode_length = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
        cur_episode_csuccess = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
        # create buffers for logging extrinsic and intrinsic rewards
        if self.alg.rnd:
            erewbuffer = deque(maxlen=100)
            irewbuffer = deque(maxlen=100)
            cur_ereward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
            cur_ireward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)

        # Ensure all parameters are in-synced
        if self.is_distributed:
            print(f"Synchronizing parameters for rank {self.gpu_global_rank}...")
            self.alg.broadcast_parameters()
            # TODO: Do we need to synchronize empirical normalizers?
            #   Right now: No, because they all should converge to the same values "asymptotically".

        # Start training
        start_iter = self.current_learning_iteration
        tot_iter = start_iter + num_learning_iterations
        best_eval_returns = 0.0
        if self.debug < 2:
            pbar = tqdm(range(start_iter, tot_iter))
        else:
            pbar = range(start_iter, tot_iter)
        for it in pbar:
            start = time.time()
            # Rollout
            if self.sweep:
                context = torch.no_grad()
            else:
                context = torch.inference_mode()
            with context:
                for _ in range(self.num_steps_per_env):
                    # Sample actions
                    actions = self.alg.act(obs, privileged_obs)
                    # Step the environment
                    obs, rewards, dones, infos = self.env.step(actions.to(self.env.device))
                    # Move to device
                    obs, rewards, dones = (obs.to(self.device), rewards.to(self.device), dones.to(self.device))
                    # perform normalization
                    obs = self.obs_normalizer(obs)
                    if self.privileged_obs_type is not None:
                        privileged_obs = self.privileged_obs_normalizer(
                            infos["observations"][self.privileged_obs_type].to(self.device)
                        )
                    else:
                        privileged_obs = obs

                    # process the step
                    self.alg.process_env_step(rewards, dones, infos)

                    # Extract intrinsic rewards (only for logging)
                    intrinsic_rewards = self.alg.intrinsic_rewards if self.alg.rnd else None

                    # book keeping
                    if self.log_dir is not None:
                        if "episode" in infos:
                            ep_infos.append(infos["episode"])
                        elif "log" in infos:
                            ep_infos.append(infos["log"])
                        # Update rewards
                        if self.alg.rnd:
                            cur_ereward_sum += rewards
                            cur_ireward_sum += intrinsic_rewards  # type: ignore
                            cur_reward_sum += rewards + intrinsic_rewards
                        else:
                            cur_reward_sum += rewards
                        # Update episode length
                        cur_episode_length += 1

                        # Update consective success
                        if "consecutive_successes" in infos['log'].keys():
                            cur_episode_csuccess = torch.maximum(cur_episode_csuccess, infos['log']['consecutive_successes'].clone().detach())
                        
                        # Clear data for completed episodes
                        # -- common
                        new_ids = (dones > 0).nonzero(as_tuple=False).squeeze(-1)
                        is_eval_env = new_ids < self.num_eval_envs # we simply use the first num_eval_envs envs as the eval envs
                        if torch.any(is_eval_env):
                            ids = new_ids[is_eval_env]
                            rewbuffer.extend(cur_reward_sum[ids].cpu().numpy().tolist())
                            lenbuffer.extend(cur_episode_length[ids].cpu().numpy().tolist())
                            csbuffer.extend(cur_episode_csuccess[ids].cpu().numpy().tolist())

                        cur_reward_sum[new_ids] = 0.0
                        cur_episode_length[new_ids] = 0.0
                        cur_episode_csuccess[new_ids] = 0.0
                        # -- intrinsic and extrinsic rewards
                        if self.alg.rnd:
                            erewbuffer.extend(cur_ereward_sum[new_ids].cpu().numpy().tolist())
                            irewbuffer.extend(cur_ireward_sum[new_ids].cpu().numpy().tolist())
                            cur_ereward_sum[new_ids] = 0
                            cur_ireward_sum[new_ids] = 0

                stop = time.time()
                collection_time = stop - start
                start = stop

                # compute returns
                if self.training_type == "rl":
                    self.alg.compute_returns(privileged_obs)

            # update policy
            loss_dict = self.alg.update()

            stop = time.time()
            learn_time = stop - start
            self.current_learning_iteration = it
            # log info
            if self.log_dir is not None and not self.disable_logs:
                # Log information
                self.log(locals())
                # Save model
                if it % self.save_interval == 0:
                    self.save(os.path.join(self.log_dir, f"model_{it}.pt"))

            # Clear episode infos
            ep_infos.clear()
            # Save code state
            if it == start_iter and not self.disable_logs:
                # obtain all the diff files
                git_file_paths = store_code_state(self.log_dir, self.git_status_repos)
                # if possible store them to wandb
                if self.logger_type in ["wandb", "neptune"] and git_file_paths:
                    for path in git_file_paths:
                        self.writer.save_file(path)
            

            if len(csbuffer) > 0:
                mean_eval_returns = statistics.mean(csbuffer)
                if mean_eval_returns > best_eval_returns:
                    best_eval_returns = mean_eval_returns
                
                if trial is not None:
                    trial.report(mean_eval_returns, it)
                    if trial.should_prune():
                        return best_eval_returns, True
        

        # Save the final model after training
        if self.log_dir is not None and not self.disable_logs:
            self.save(os.path.join(self.log_dir, f"model_{self.current_learning_iteration}.pt"))
        
        return best_eval_returns, False


        

    def log(self, locs: dict, width: int = 80, pad: int = 35):
        # Compute the collection size
        collection_size = self.num_steps_per_env * self.env.num_envs * self.gpu_world_size
        # Update total time-steps and time
        self.tot_timesteps += collection_size
        self.tot_time += locs["collection_time"] + locs["learn_time"]
        iteration_time = locs["collection_time"] + locs["learn_time"]

        # -- Episode info
        ep_string = ""
        if locs["ep_infos"]:
            for key in locs["ep_infos"][0]:
                if key == "consecutive_successes":
                    continue
                infotensor = torch.tensor([], device=self.device)
                for ep_info in locs["ep_infos"]:
                    # handle scalar and zero dimensional tensor infos
                    if key not in ep_info:
                        continue
                    
                    if not isinstance(ep_info[key], torch.Tensor):
                        ep_info[key] = torch.Tensor([ep_info[key]])
                    if len(ep_info[key].shape) == 0:
                        ep_info[key] = ep_info[key].unsqueeze(0)
                    infotensor = torch.cat((infotensor, ep_info[key].to(self.device)))
                value = torch.mean(infotensor)
                # log to logger and terminal
                if "/" in key:
                    self.writer.add_scalar(key, value, locs["it"])
                    ep_string += f"""{f'{key}:':>{pad}} {value:.4f}\n"""
                else:
                    self.writer.add_scalar("Episode/" + key, value, locs["it"])
                    ep_string += f"""{f'Mean episode {key}:':>{pad}} {value:.4f}\n"""

        mean_std = self.alg.policy.action_std.mean()
        fps = int(collection_size / (locs["collection_time"] + locs["learn_time"]))

        # -- Losses
        for key, value in locs["loss_dict"].items():
            self.writer.add_scalar(f"Loss/{key}", value, locs["it"])
        self.writer.add_scalar("Loss/learning_rate", self.alg.learning_rate, locs["it"])

        # -- Policy
        self.writer.add_scalar("Policy/mean_noise_std", mean_std.item(), locs["it"])
        
        # -- Scalar
        self.writer.add_scalar("Scalar/running_mean_mean", self.alg.value_normalizer.running_mean_mean, locs["it"])
        self.writer.add_scalar("Scalar/running_mean_median", self.alg.value_normalizer.running_mean_median, locs["it"])
        self.writer.add_scalar("Scalar/running_mean_min", self.alg.value_normalizer.running_mean_min, locs["it"])
        self.writer.add_scalar("Scalar/running_mean_max", self.alg.value_normalizer.running_mean_max, locs["it"])
        self.writer.add_scalar("Scalar/running_variance_mean", self.alg.value_normalizer.running_variance_mean, locs["it"])
        self.writer.add_scalar("Scalar/running_variance_median", self.alg.value_normalizer.running_variance_median, locs["it"])
        self.writer.add_scalar("Scalar/running_variance_min", self.alg.value_normalizer.running_variance_min, locs["it"])
        self.writer.add_scalar("Scalar/running_variance_max", self.alg.value_normalizer.running_variance_max, locs["it"])

        # -- Performance
        self.writer.add_scalar("Perf/total_fps", fps, locs["it"])
        self.writer.add_scalar("Perf/collection time", locs["collection_time"], locs["it"])
        self.writer.add_scalar("Perf/learning_time", locs["learn_time"], locs["it"])

        # -- Training
        if len(locs["rewbuffer"]) > 0:
            # separate logging for intrinsic and extrinsic rewards
            if self.alg.rnd:
                self.writer.add_scalar("Rnd/mean_extrinsic_reward", statistics.mean(locs["erewbuffer"]), locs["it"])
                self.writer.add_scalar("Rnd/mean_intrinsic_reward", statistics.mean(locs["irewbuffer"]), locs["it"])
                self.writer.add_scalar("Rnd/weight", self.alg.rnd.weight, locs["it"])
            # everything else
            self.writer.add_scalar("Train/mean_reward", statistics.mean(locs["rewbuffer"]), locs["it"])
            self.writer.add_scalar("Train/mean_episode_length", statistics.mean(locs["lenbuffer"]), locs["it"])
            if len(locs["csbuffer"])> 0:
                self.writer.add_scalar("Train/mean_consecutive_success", statistics.mean(locs["csbuffer"]), locs["it"])
            if self.logger_type != "wandb":  # wandb does not support non-integer x-axis logging
                self.writer.add_scalar("Train/mean_reward/time", statistics.mean(locs["rewbuffer"]), self.tot_time)
                self.writer.add_scalar(
                    "Train/mean_episode_length/time", statistics.mean(locs["lenbuffer"]), self.tot_time
                )
        
        str = f" \033[1m Learning iteration {locs['it']}/{locs['tot_iter']} \033[0m "

        if len(locs["rewbuffer"]) > 0:
            log_string = (
                f"""{'#' * width}\n"""
                f"""{str.center(width, ' ')}\n\n"""
                f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                    'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n"""
            )
            # -- Losses
            for key, value in locs["loss_dict"].items():
                log_string += f"""{f'Mean {key} loss:':>{pad}} {value:.4f}\n"""
            # -- Rewards
            if self.alg.rnd:
                log_string += (
                    f"""{'Mean extrinsic reward:':>{pad}} {statistics.mean(locs['erewbuffer']):.2f}\n"""
                    f"""{'Mean intrinsic reward:':>{pad}} {statistics.mean(locs['irewbuffer']):.2f}\n"""
                )
            log_string += f"""{'Mean reward:':>{pad}} {statistics.mean(locs['rewbuffer']):.2f}\n"""
            # -- episode info
            log_string += f"""{'Mean episode length:':>{pad}} {statistics.mean(locs['lenbuffer']):.2f}\n"""
            if len(locs["csbuffer"]) > 0:
                log_string += f"""{'Mean consecutive success:':>{pad}} {statistics.mean(locs['csbuffer']):.2f}\n"""
        else:
            log_string = (
                f"""{'#' * width}\n"""
                f"""{str.center(width, ' ')}\n\n"""
                f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                    'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n"""
            )
            for key, value in locs["loss_dict"].items():
                log_string += f"""{f'{key}:':>{pad}} {value:.4f}\n"""

        log_string += ep_string
        log_string += (
            f"""{'-' * width}\n"""
            f"""{'Total timesteps:':>{pad}} {self.tot_timesteps}\n"""
            f"""{'Iteration time:':>{pad}} {iteration_time:.2f}s\n"""
            f"""{'Time elapsed:':>{pad}} {time.strftime("%H:%M:%S", time.gmtime(self.tot_time))}\n"""
            f"""{'ETA:':>{pad}} {time.strftime(
                "%H:%M:%S",
                time.gmtime(
                    self.tot_time / (locs['it'] - locs['start_iter'] + 1)
                    * (locs['start_iter'] + locs['num_learning_iterations'] - locs['it'])
                )
            )}\n"""
        )
        if self.debug == 2:
            print(log_string)
        elif self.debug == 1:
            mean_rew = statistics.mean(locs['rewbuffer']) if len(locs['rewbuffer']) > 0 else 0.0
            mean_cs = statistics.mean(locs['csbuffer']) if len(locs['csbuffer']) > 0 else 0.0
            
            locs['pbar'].set_description(f"rew: {mean_rew:.2f}, cs: {mean_cs:.2f}")
        

        # video logging
        if self.logger_type == "wandb" and self.upload_video:
            if len(os.listdir(self.video_log_dir)) > 0:
                f = sorted(os.listdir(self.video_log_dir))[-1]
                if f not in self.uploaded_videos:
                    self.writer.wandb_add_video(
                        tag='video', video_f=os.path.join(self.video_log_dir, f), global_step=locs['it']
                    )
                self.uploaded_videos.append(f)



    def save(self, path: str, infos=None):
        # -- Save model
        saved_dict = {
            "model_state_dict": self.alg.policy.state_dict(),
            "optimizer_state_dict": self.alg.optimizer.state_dict(),
            "iter": self.current_learning_iteration,
            "infos": infos,
        }
        # -- Save RND model if used
        if self.alg.rnd:
            saved_dict["rnd_state_dict"] = self.alg.rnd.state_dict()
            saved_dict["rnd_optimizer_state_dict"] = self.alg.rnd_optimizer.state_dict()
        # -- Save observation normalizer if used
        if self.empirical_normalization:
            saved_dict["obs_norm_state_dict"] = self.obs_normalizer.state_dict()
            saved_dict["privileged_obs_norm_state_dict"] = self.privileged_obs_normalizer.state_dict()
        
        # -- Save value normalizer if used
        if self.value_normalization:
            saved_dict["value_norm_state_dict"] = self.alg.value_normalizer.state_dict()

        # save model
        torch.save(saved_dict, path)

        # upload model to external logging service
        if self.logger_type in ["neptune", "wandb"] and not self.disable_logs:
            self.writer.save_model(path, self.current_learning_iteration)

    def load(self, path: str, load_optimizer: bool = True):
        loaded_dict = torch.load(path, weights_only=False)
        # -- Load model
        resumed_training = self.alg.policy.load_state_dict(loaded_dict["model_state_dict"])
        # -- Load RND model if used
        if self.alg.rnd:
            self.alg.rnd.load_state_dict(loaded_dict["rnd_state_dict"])
        # -- Load observation normalizer if used
        if self.empirical_normalization:
            if resumed_training:
                # if a previous training is resumed, the actor/student normalizer is loaded for the actor/student
                # and the critic/teacher normalizer is loaded for the critic/teacher
                self.obs_normalizer.load_state_dict(loaded_dict["obs_norm_state_dict"])
                self.privileged_obs_normalizer.load_state_dict(loaded_dict["privileged_obs_norm_state_dict"])
            else:
                # if the training is not resumed but a model is loaded, this run must be distillation training following
                # an rl training. Thus the actor normalizer is loaded for the teacher model. The student's normalizer
                # is not loaded, as the observation space could differ from the previous rl training.
                self.privileged_obs_normalizer.load_state_dict(loaded_dict["obs_norm_state_dict"])
        
        if self.value_normalization:
            if resumed_training:
                self.alg.value_normalizer.load_state_dict(loaded_dict['value_norm_state_dict'])
        
        
        # -- load optimizer if used
        if load_optimizer and resumed_training:
            # -- algorithm optimizer
            self.alg.optimizer.load_state_dict(loaded_dict["optimizer_state_dict"])
            # -- RND optimizer if used
            if self.alg.rnd:
                self.alg.rnd_optimizer.load_state_dict(loaded_dict["rnd_optimizer_state_dict"])
        # -- load current learning iteration
        if resumed_training:
            self.current_learning_iteration = loaded_dict["iter"]
        return loaded_dict["infos"]

    def get_inference_policy(self, device=None):
        self.eval_mode()  # switch to evaluation mode (dropout for example)
        if device is not None:
            self.alg.policy.to(device)
        policy = self.alg.policy.act_inference
        if self.cfg["empirical_normalization"]:
            if device is not None:
                self.obs_normalizer.to(device)
            policy = lambda x: self.alg.policy.act_inference(self.obs_normalizer(x))  # noqa: E731
        return policy

    def train_mode(self):
        # -- PPO
        self.alg.policy.train()
        # -- RND
        if self.alg.rnd:
            self.alg.rnd.train()
        # -- Normalization
        if self.empirical_normalization:
            self.obs_normalizer.train()
            self.privileged_obs_normalizer.train()

    def eval_mode(self):
        # -- PPO
        self.alg.policy.eval()
        # -- RND
        if self.alg.rnd:
            self.alg.rnd.eval()
        # -- Normalization
        if self.empirical_normalization:
            self.obs_normalizer.eval()
            self.privileged_obs_normalizer.eval()

    def add_git_repo_to_log(self, repo_file_path):
        self.git_status_repos.append(repo_file_path)

    """
    Helper functions.
    """

    def _configure_multi_gpu(self):
        """Configure multi-gpu training."""
        # check if distributed training is enabled
        self.gpu_world_size = int(os.getenv("WORLD_SIZE", "1"))
        self.is_distributed = self.gpu_world_size > 1

        # if not distributed training, set local and global rank to 0 and return
        if not self.is_distributed:
            self.gpu_local_rank = 0
            self.gpu_global_rank = 0
            self.multi_gpu_cfg = None
            return

        # get rank and world size
        self.gpu_local_rank = int(os.getenv("LOCAL_RANK", "0"))
        self.gpu_global_rank = int(os.getenv("RANK", "0"))

        # make a configuration dictionary
        self.multi_gpu_cfg = {
            "global_rank": self.gpu_global_rank,  # rank of the main process
            "local_rank": self.gpu_local_rank,  # rank of the current process
            "world_size": self.gpu_world_size,  # total number of processes
        }

        # check if user has device specified for local rank
        if self.device != f"cuda:{self.gpu_local_rank}":
            raise ValueError(
                f"Device '{self.device}' does not match expected device for local rank '{self.gpu_local_rank}'."
            )
        # validate multi-gpu configuration
        if self.gpu_local_rank >= self.gpu_world_size:
            raise ValueError(
                f"Local rank '{self.gpu_local_rank}' is greater than or equal to world size '{self.gpu_world_size}'."
            )
        if self.gpu_global_rank >= self.gpu_world_size:
            raise ValueError(
                f"Global rank '{self.gpu_global_rank}' is greater than or equal to world size '{self.gpu_world_size}'."
            )

        # initialize torch distributed
        torch.distributed.init_process_group(backend="nccl", rank=self.gpu_global_rank, world_size=self.gpu_world_size)
        # set device to the local rank
        torch.cuda.set_device(self.gpu_local_rank)
    
    
    def close(self):
        """Cleans up the runner, environment, and logs, and releases GPU memory."""
        print("Closing OnPolicyRunner...")
        # 1. Close the environment
        if self.env is not None:
            self.env.close()

        # 2. Close the logger
        if self.writer is not None:
            self.writer.close()

        # 3. Destroy the distributed process group
        if self.is_distributed:
            torch.distributed.destroy_process_group()

        # 4. Explicitly delete large torch objects
        del self.alg
        del self.obs_normalizer
        del self.privileged_obs_normalizer
        
        # 5. Run Python's garbage collector and clear PyTorch's CUDA cache
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        
        print("OnPolicyRunner closed successfully.")
