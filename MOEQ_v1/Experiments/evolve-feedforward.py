###########################################################################
# Gabriel Matos Leite, PhD candidate (email: gmatos@cos.ufrj.br)
# March 30, 2023
###########################################################################
# Copyright (c) 2023, Gabriel Matos Leite
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in
#      the documentation and/or other materials provided with the
#      distribution
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS USING 
# THE CREATIVE COMMONS LICENSE: CC BY-NC-ND "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import print_function

import os
import neat
import numpy as np
from tqdm import trange
from utils.sort_population_mo_simple import select_n_best, select_n_best_hvc
from pymoo.indicators.hv import HV
import pickle
import warnings
import copy
from datetime import datetime
from collections import namedtuple
import argparse

warnings.filterwarnings("ignore")

valid_envs = ['DST']

def load_env(name, **kwargs):
    EnvConfig = namedtuple("EnvConfig", "env hv extras")
    extras = None
    if name == "dst":
        from envs.DST.DeepSeaTreasureEnv import DeepSeaTreasureEnv
        hv = HV(ref_point=np.array([25, 0]))
        env = DeepSeaTreasureEnv
    else:
        raise Exception("Inexistent environment")
    return EnvConfig(env, hv, extras)


def eval_genomes(ind, config, **fitness_kwargs):
    env_class = fitness_kwargs.get("env")
    extras = fitness_kwargs.get("extras")
    if extras is None:
        env = env_class()
    else:
        env = env_class(*extras)
    episode_length = fitness_kwargs.get("episode_length")

    if not isinstance(ind, list):
        genomes = [(None, ind)]
    else:
        genomes = ind

    for _, genome in genomes:
        env.reset()

        episode_length = fitness_kwargs.get("episode_length")

        net = neat.nn.FeedForwardNetwork.create(genome, config)

        st = env.get_state()
        if isinstance(st, list) and len(st) > 1:
            trajectory = -1 * np.ones((episode_length + 1, len(st)), dtype=int)
        else:
            trajectory = -1 * np.ones(episode_length + 1, dtype=int)
        trajectory[0] = st
        action_trajectory = -1 * np.ones(episode_length, dtype=int)
        q_trajectory = []
        r_trajectory = []
        
        for i in range(episode_length):
            if isinstance(st, list) and len(st) > 1:
                q_values = net.activate(st)
            else:
                q_values = net.activate([st])
            q_values = np.array(q_values)
            action = np.argmax(q_values) #greedy policy

            action_trajectory[i] = action
            q_trajectory.append(q_values[action].tolist())

            st, reward, is_terminal = env.step(action)

            trajectory[i + 1] = st
            r_trajectory.append(reward)
            if is_terminal:
                break
        
        q_trajectory = np.array(q_trajectory)
        r_trajectory = np.array(r_trajectory)
        time_normalizer = np.zeros_like(r_trajectory)
        time_normalizer[1:,0] = 1
        r_trajectory += time_normalizer.cumsum(axis=0)
        prod = np.ones_like(q_trajectory)
        prod[1:] = 0.9
        prod = np.cumprod(prod).reshape(-1,1)
        R = np.zeros_like(r_trajectory)
        Q = np.zeros_like(q_trajectory)
        for i, v in enumerate(r_trajectory):
            if i == 0:
                Q[i] = np.sum(q_trajectory * prod.flatten(), axis=0)
            else:
                Q[i] = np.sum(q_trajectory[i:] * prod[:-i].flatten(), axis=0)

        w_preference = np.linalg.pinv(r_trajectory).dot(Q)
        w_preference = np.abs(w_preference)
        C = w_preference.sum()
        w_preference /= C
        w_preference = 1 - w_preference
        w_preference = np.nan_to_num(w_preference)

        genome.trajectory = trajectory
        genome.preference = w_preference
        genome.q_values = q_trajectory
        genome.a_trajectory = action_trajectory

        genome.fitness = reward
        if config.fitness_criterion == "max":
            genome.fitness *= -1
        
        if len(genomes) == 1:
            return genome

def run_by_gen(config_file, episode_length, env_name, total_gen=1000, parallel=0, env_kwargs={}, ht=0.0, adpt=0.0, use_hvc=True):
    memory_best = {}
    hypervolume_history = []
    species_history = []
    trajectories_parents_history = []
    trajectories_offspring_history = []

    # Load configuration.
    config = neat.Config(neat.DefaultGenome, config_file)
    max_solutions = config.pop_size

    env_config = load_env(env_name, **env_kwargs)
    hv = env_config.hv
    extras = env_config.extras
    fitness_kwargs = {"env":env_config.env, "episode_length":episode_length, "extras":extras}

    if parallel:
        pe = neat.ParallelEvaluator(int(parallel), eval_genomes)
        p = neat.Population(config, pe.evaluate, **fitness_kwargs)
    else:
        p = neat.Population(config, eval_genomes, **fitness_kwargs)
    
    if extras is None:
        env = env_config.env()
    else:
        env = env_config.env(*extras)
    try:
        heatmap = np.zeros_like(env.map)
        include_heatmap = True
    except:
        heatmap = None
        include_heatmap = False

    best_hv = 0
    with trange(total_gen) as t:
        for i in t:
            t.set_description('GEN {0}'.format(i+1))

            solutions, current_parents, current_offspring =\
                 p.run_once(total_gen, max_solutions=max_solutions, adpt_scale=adpt, alpha=ht, use_hvc=use_hvc, **fitness_kwargs)
            hv_current_points = []
            for _, genome in p.population.items():
                hv_current_points.append(genome.fitness)
            
            if include_heatmap:
                trajectories_parents_gen = []
                factor = env.num_cols * env.num_rows
                for _, genome in current_parents.items():
                    trajectories_parents_gen.append(genome.trajectory.tolist())
                    for st in genome.trajectory:
                        if st == -1:
                            break
                        row = int(np.round(st * factor) - 1) // env.num_cols
                        col = int(np.round(st * factor) - 1) % env.num_cols
                        heatmap[row, col] += 1
                trajectories_parents_history.append(trajectories_parents_gen)

                trajectories_offspring_gen = []
                for _, genome in current_offspring.items():
                    trajectories_offspring_gen.append(genome.trajectory.tolist())
                    for st in genome.trajectory:
                        if st == -1:
                            break
                        row = int(np.round(st * factor) - 1) // env.num_cols
                        col = int(np.round(st * factor) - 1) % env.num_cols
                        heatmap[row, col] += 1
                trajectories_offspring_history.append(trajectories_offspring_gen)

            hv_current_points = np.array(hv_current_points)
            hv_current = hv.do(hv_current_points)

            hv_points = np.array([list(f) for f in p.memory.keys()])
            hv_value = hv.do(hv_points)

            best_hv = hv_value

            hypervolume_history.append([hv_current, best_hv, np.array(hv_points)])
        
            t.set_postfix(EL=episode_length, SIG=list(p.population.values())[0].sigma, \
                PO = len(p.memory.items()), BHV = str(best_hv), AHV = str(hv_current))
            

    for _, solution in solutions.items():
        fit = tuple(solution.fitness)
        if fit not in memory_best:
            memory_best[fit] = copy.deepcopy(solution)

    print('\nSolutions:')
    front = []
    
    memory_best_list = list(map(lambda x: (*x.fitness, x.key), p.memory.values()))
    memory_best_list.sort(key=lambda x: x[0])
    for solution in memory_best_list:
        print("Time {0}, Reward {1}, Genome id {2}".format(*solution))
        front.append(list(solution))
    
    return hypervolume_history, np.array(front), p.memory, [np.array(trajectories_parents_history), np.array(trajectories_offspring_history)], heatmap


def main(config_file, episode_length, env_name, n_exp=10, n_gen=100, local_dir="", parallel=0, env_kwargs={}, ht=0.0, adpt=0.0, use_hvc=True):
    results = {}
    param_info_name = '{}_{}_{}'
    if ht > 0 and adpt > 0:
        param_info_name = param_info_name.format(datetime.now().strftime("%d-%m-%Y_%H-%M"), 'adpt_{}'.format(str(adpt).replace('.', '-')),  'ht_{}'.format(str(ht).replace('.', '-')))
    elif adpt > 0:
        param_info_name = param_info_name.format(datetime.now().strftime("%d-%m-%Y_%H-%M"), 'adpt_{}'.format(str(adpt).replace('.', '-')),  'ps')
    elif ht > 0:
        param_info_name = param_info_name.format(datetime.now().strftime("%d-%m-%Y_%H-%M"), 'no_adpt',  'ht_{}'.format(str(ht).replace('.', '-')))
    else:
        param_info_name = param_info_name.format(datetime.now().strftime("%d-%m-%Y_%H-%M"), 'no_adpt',  'ps')

    if use_hvc:
        param_info_name = 'hvc_' + param_info_name

    if 'residential' in env_kwargs and env_kwargs['residential']:
        filename = os.path.join(local_dir, 'results_residential_{0}_.pickle'.format(param_info_name))
    else:
        filename = os.path.join(local_dir, 'results_{0}.pickle'.format(param_info_name))
    for i in range(n_exp):
        print("Run {0}".format(i))
        assert n_exp > 0 and n_gen > 0
        hypervolume_history, front, memory_best, trajectories_history, heatmap =\
                run_by_gen(config_file, episode_length, env_name, total_gen=n_gen,\
                     parallel=parallel, env_kwargs=env_kwargs, ht=ht, adpt=adpt, use_hvc=use_hvc)
        if heatmap is None:
            result = {'hvs': hypervolume_history, 'solutions':np.array(front), 'solutions_dict':memory_best, 'trajectories':trajectories_history, 'heatmap': None}
        else:
            result = {'hvs': hypervolume_history, 'solutions':np.array(front), 'solutions_dict':memory_best,\
                 'trajectories':trajectories_history, 'heatmap': heatmap.copy()}
        results[i+1] = result
        with open(filename, 'wb') as f:
            pickle.dump(results, f)
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train MOEQ')
    parser.add_argument('--env', dest='env', action='store', default="dst")
    parser.add_argument('--ngen', dest='n_gen', action='store', type=int, default=100)
    parser.add_argument('--nrun', dest='n_run', action='store', type=int, default=1)
    parser.add_argument('--length', dest='episode_length', action='store', type=int, default=1)
    parser.add_argument('--parallel', dest='parallel', action='store', type=int, default=0)
    parser.add_argument('--ht', dest='ht', action='store', type=float, default=0.0)
    parser.add_argument('--adpt', dest='adpt', action='store', type=float, default=0.0)
    parser.add_argument('--no-hvc', dest='use_hvc', action='store_false')
    
    args = parser.parse_args()
    assert args.n_run > 0
    assert args.n_gen > 0
    assert args.episode_length > 0
    assert args.parallel >= 0
    assert args.env.upper() in valid_envs

    # Determine path to configuration file. This path manipulation is
    # here so that the script will run successfully regardless of the
    # current working directory.
    local_dir = os.path.dirname(__file__)

    results_path = os.path.join(local_dir, 'envs/{}/results'.format(args.env.upper()))
    if not os.path.isdir(results_path):
        os.mkdir(results_path)

    folder_name = "{0}-{1}".format(args.env.upper(), datetime.now().strftime("%d-%m-%Y"))
    folder_name = os.path.join(results_path, folder_name)

    if not os.path.isdir(folder_name):
        os.mkdir(folder_name)

    config_path = os.path.join(local_dir, 'config-feedforward-{0}'.format(args.env.lower()))

    env_kwargs = {}
    main(config_path, args.episode_length, args.env, n_exp=args.n_run, n_gen=args.n_gen,\
         local_dir=folder_name, parallel=args.parallel, env_kwargs=env_kwargs, ht=args.ht, adpt=args.adpt, use_hvc=args.use_hvc)