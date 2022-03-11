# %%

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Dec  2 09:10:12 2020

@author: Harry, Helena, and Linh
"""

import torch
import torch.nn as nn
import torch.optim as optim
import torch_geometric
import torch_scatter

import e3nn
from e3nn import rs, o3
from e3nn.point.data_helpers import DataPeriodicNeighbors
from e3nn.networks import GatedConvParityNetwork
from e3nn.kernel_mod import Kernel
from e3nn.point.message_passing import Convolution

import pymatgen as mg
import pymatgen.io
from pymatgen.core.structure import Structure
from pymatgen.ext.matproj import MPRester
import pymatgen.analysis.magnetism.analyzer as pg
import numpy as np
import pickle
from mendeleev import element
import matplotlib.pyplot as plt

from sklearn.metrics import average_precision_score
from sklearn.metrics import classification_report
from sklearn.metrics import f1_score
from sklearn.metrics import accuracy_score

import io
import random
import math
import sys
import time
import os
import datetime


# %% Process Materials Project Data
order_list_mp = []
structures_list_mp = []
formula_list_mp = []
sites_list = []
id_list_mp = []
y_values_mp = []
order_encode = {"NM": 0, "AFM": 1, "FM": 2, "FiM": 2}

magnetic_atoms = ['Ga', 'Tm', 'Y', 'Dy', 'Nb', 'Pu', 'Th', 'Er', 'U',
                  'Cr', 'Sc', 'Pr', 'Re', 'Ni', 'Np', 'Nd', 'Yb', 'Ce',
                  'Ti', 'Mo', 'Cu', 'Fe', 'Sm', 'Gd', 'V', 'Co', 'Eu',
                  'Ho', 'Mn', 'Os', 'Tb', 'Ir', 'Pt', 'Rh', 'Ru']

# m = MPRester(api_key='PqU1TATsbzHEOkSX', endpoint=None, notify_db_version=True, include_user_agent=True)
m = MPRester(endpoint=None, include_user_agent=True)
# get structures containing magnetic atoms
structures = m.query(criteria={"elements": {"$in": magnetic_atoms}, 'blessed_tasks.GGA+U Static': {
                     '$exists': True}}, properties=["material_id", "pretty_formula", "structure", "blessed_tasks", "nsites"])

structures_copy = structures.copy()
for struc in structures_copy:
    if len(struc["structure"]) > 250:
        structures.remove(struc)
        print("MP Structure Deleted")

# %%
# reorder structures with respect to magnetic order
order_list = []  # list of magnetic orders
for i in range(len(structures)):
    order = pg.CollinearMagneticStructureAnalyzer(structures[i]["structure"])
    order_list.append(order.ordering.name)  # i.e. FM, AM, NM
id_NM = []
id_FM = []
id_AFM = []
for i in range(len(structures)):
    if order_list[i] == 'NM':
        id_NM.append(i)
    if order_list[i] == 'AFM':
        id_AFM.append(i)
    if order_list[i] == 'FM' or order_list[i] == 'FiM':
        id_FM.append(i)
np.random.shuffle(id_FM)
np.random.shuffle(id_NM)
np.random.shuffle(id_AFM)
id_AFM, id_AFM_to_delete = np.split(id_AFM, [int(len(id_AFM))])
id_NM, id_NM_to_delete = np.split(id_NM, [int(1.2*len(id_AFM))])
id_FM, id_FM_to_delete = np.split(id_FM, [int(1.2*len(id_AFM))])

structures_mp = [structures[i] for i in id_NM] + [structures[j]
                                                  for j in id_FM] + [structures[k] for k in id_AFM]
np.random.shuffle(structures_mp)


for structure in structures_mp:
    analyzed_structure = pg.CollinearMagneticStructureAnalyzer(
        structure["structure"])
    order_list_mp.append(analyzed_structure.ordering)
    structures_list_mp.append(structure["structure"])
    formula_list_mp.append(structure["pretty_formula"])
    id_list_mp.append(structure["material_id"])
    sites_list.append(structure["nsites"])

for order in order_list_mp:
    y_values_mp.append(order_encode[order.name])

torch.set_default_dtype(torch.float64)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

params = {'len_embed_feat': 64,
          'num_channel_irrep': 32,
          'num_e3nn_layer': 2,
          'max_radius': 5,
          'num_basis': 10,
          'adamw_lr': 0.005,
          'adamw_wd': 0.03
          }

# Used for debugging
identification_tag = "1:1:1.1 Relu wd:0.03 4 Linear"
cost_multiplier = 1.0

print('Length of embedding feature vector: {:3d} \n'.format(params.get('len_embed_feat')) +
      'Number of channels per irreducible representation: {:3d} \n'.format(params.get('num_channel_irrep')) +
      'Number of tensor field convolution layers: {:3d} \n'.format(params.get('num_e3nn_layer')) +
      'Maximum radius: {:3.1f} \n'.format(params.get('max_radius')) +
      'Number of basis: {:3d} \n'.format(params.get('num_basis')) +
      'AdamW optimizer learning rate: {:.4f} \n'.format(params.get('adamw_lr')) +
      'AdamW optimizer weight decay coefficient: {:.4f}'.format(
          params.get('adamw_wd'))
      )


run_name = (time.strftime("%y%m%d-%H%M", time.localtime()))


structures = structures_list_mp
y_values = y_values_mp
id_list = id_list_mp


species = set()
count = 0
for struct in structures[:]:
    try:
        species = species.union(list(set(map(str, struct.species))))
        count += 1
    except:
        print(count)
        count += 1
        continue
species = sorted(list(species))
print("Distinct atomic species ", len(species))

len_element = 118
atom_types_dim = 3*len_element
embedding_dim = params['len_embed_feat']
lmax = 1
# Roughly the average number (over entire dataset) of nearest neighbors for a given atom
n_norm = 35

Rs_in = [(45, 0, 1)]  # num_atom_types scalars (L=0) with even parity
Rs_out = [(3, 0, 1)]  # len_dos scalars (L=0) with even parity

# model_kwargs = {
#     "convolution": Convolution,
#     "kernel": Kernel,
#     "Rs_in": Rs_in,
#     "Rs_out": Rs_out,
#     # number of channels per irrep (differeing L and parity)
#     "mul": params['num_channel_irrep'],
#     "layers": params['num_e3nn_layer'],
#     "max_radius": params['max_radius'],
#     "lmax": lmax,
#     "number_of_basis": params['num_basis']
# }
# print(model_kwargs)


# class AtomEmbeddingAndSumLastLayer(torch.nn.Module):
#     def __init__(self, atom_type_in, atom_type_out, model):
#         super().__init__()
#         self.linear = torch.nn.Linear(atom_type_in, 128)
#         self.model = model
#         self.relu = torch.nn.ReLU()
#         self.linear2 = torch.nn.Linear(128, 96)
#         self.linear3 = torch.nn.Linear(96, 64)
#         self.linear4 = torch.nn.Linear(64, 45)
#         #self.linear5 = torch.nn.Linear(45, 32)
#         #self.softmax = torch.nn.LogSoftmax(dim=1)

#     def forward(self, x, *args, batch=None, **kwargs):
#         output = self.linear(x)
#         output = self.relu(output)
#         print(f"Input: {x}")
#         output = self.linear2(output)
#         output = self.relu(output)
#         output = self.linear3(output)
#         output = self.relu(output)
#         output = self.linear4(output)
#         #output = self.linear5(output)
#         output = self.relu(output)
#         output = self.model(output, *args, **kwargs)
#         if batch is None:
#             N = output.shape[0]
#             batch = output.new_ones(N)
#         output = torch_scatter.scatter_add(output, batch, dim=0)
#         print(f"Output: {output}")
#         #output = self.softmax(output)
#         return output


# model = AtomEmbeddingAndSumLastLayer(
#     atom_types_dim, embedding_dim, GatedConvParityNetwork(**model_kwargs))
# opt = torch.optim.AdamW(
#     model.parameters(), lr=params['adamw_lr'], weight_decay=params['adamw_wd'])

data = []
count = 0
indices_to_delete = []
for i, struct in enumerate(structures):
    try:
        print(
            f"Encoding sample {i+1:5d}/{len(structures):5d}", end="\r", flush=True)
        input = torch.zeros(len(struct), 3*len_element)
        for j, site in enumerate(struct):
            input[j, int(element(str(site.specie)).atomic_number)
                  ] = element(str(site.specie)).atomic_radius
            #input[j, len_element + int(element(str(site.specie)).atomic_number) +1] = element(str(site.specie)).atomic_weight
            input[j, len_element + int(element(str(site.specie)).atomic_number) +
                  1] = element(str(site.specie)).en_pauling  # error?
            input[j, 2*len_element + int(element(str(site.specie)).atomic_number) + 1] = element(
                str(site.specie)).dipole_polarizability
        data.append(DataPeriodicNeighbors(
            x=input, Rs_in=None,
            pos=torch.tensor(struct.cart_coords.copy()), lattice=torch.tensor(struct.lattice.matrix.copy()),
            r_max=params['max_radius'],
            y=(torch.tensor([y_values[i]])).to(torch.long),
            n_norm=n_norm,
        ))

        count += 1
    except Exception as e:
        indices_to_delete.append(i)
        print(f"Error: {count} {e}", end="\n")
        count += 1
        continue


struc_dictionary = dict()
for i in range(len(structures)):
    struc_dictionary[i] = structures[i]

id_dictionary = dict()
for i in range(len(id_list)):
    id_dictionary[i] = id_list[i]

for i in indices_to_delete:
    del struc_dictionary[i]
    del id_dictionary[i]

structures2 = []
for i in range(len(structures)):
    if i in struc_dictionary.keys():
        structures2.append(struc_dictionary[i])
structures = structures2

id2 = []
for i in range(len(id_list)):
    if i in id_dictionary.keys():
        id2.append(id_dictionary[i])
id_list = id2

compound_list = []
for i, struc in enumerate(structures):
    str_struc = (str(struc))
    count = 0
    while str_struc[count] != ":":
        count += 1
    str_struc = str_struc[count+2:]
    count = 0
    while str_struc[count:count+3] != "abc":
        count += 1
    str_struc = str_struc[:count]
    compound_list.append(str_struc)

torch.save(data, run_name+'_data.pt')
