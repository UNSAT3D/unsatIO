"""
Classes for creating torch dataloaders
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import h5py
import lightning as L
import numpy as np
import torch
from torch.utils.data import ConcatDataset, DataLoader, Dataset, random_split


@dataclass
class DataSelection:
    """
    DataSelection object expressing which subset of data to use.

    Args:
        sample_list: list of sample names to use
        height_range: tuple of (min, max) height indices to use
        day_range: tuple of (min, max) day indices to use
        dimension: number of spatial dimensions (2 or 3)
    """

    sample_list: List[str]
    height_range: Tuple[int, int]
    day_range: Tuple[int, int]
    dimension: int

    @property
    def num_samples(self):
        return len(self.sample_list)

    @property
    def num_heights(self):
        return self.height_range[1] - self.height_range[0]

    @property
    def num_days(self):
        return self.day_range[1] - self.day_range[0]

    @property
    def points_per_sample(self):
        if self.dimension == 2:
            return self.num_heights * self.num_days
        else:
            return self.num_days

    @property
    def num_points(self):
        return self.num_samples * self.points_per_sample


class XRayDataset(Dataset):
    """
    Dataset representing a selection of the total data (train/validation/test split)

    Args:
        hdf5_path: path to hdf5 file containing all data
        data_selection: DataSelection object expressing which subset of data to use
        name: name of dataset (train/validation/test)
        patch_size (tuple): size of the patch to extract
        patch_border (tuple): size of the border of a patch to exclude from the loss
        shuffle: whether to shuffle the patch
        dimension: number of spatial dimensions (2 or 3)
    """

    def __init__(
        self,
        hdf5_path: str,
        data_selection: DataSelection,
        name: str,
        patch_size: Optional[Tuple[int, ...]],
        patch_border: Optional[Tuple[int, ...]],
        dimension: int,
        shuffle: bool = True,
    ):
        self.name = name
        self.hdf5_path = hdf5_path
        self.hdf5_file = None  # Has to be opened in __getitem__ to be picklable
        self.selection = data_selection
        self.patch_size = patch_size
        self.patch_border = patch_border
        self.shuffle = True
        self.dimension = dimension

    def __len__(self):
        return self.selection.num_points

    def __getitem__(self, idx):
        """
        Get data and label for a single point in the dataset, given its index.
        This is used in the dataloader to construct batches.
        """
        if not self.hdf5_file:
            self.hdf5_file = h5py.File(self.hdf5_path, 'r')

        # Extract sample, day and height indices from the overall index using modular arithmetic
        (sample_idx, data_idx) = divmod(idx, self.selection.points_per_sample)
        sample_name = self.selection.sample_list[sample_idx]

        (day_idx, height_idx) = divmod(data_idx, self.selection.num_heights)
        day_idx += self.selection.day_range[0]
        height_idx += self.selection.height_range[0]

        data = self.hdf5_file[sample_name]['data'][day_idx]
        labels = self.hdf5_file[sample_name]['labels'][day_idx]
        if self.dimension == 2:
            data = data[height_idx]
            labels = labels[height_idx]

        # Extract a patch if specified
        init_shape = data.shape
        patch_starts = []
        if self.patch_size is not None:
            init_shape = data.shape
            max_starts = [init_shape[i] - self.patch_size[i] for i in range(self.dimension)]
            if self.shuffle:
                patch_starts = [np.random.randint(0, max_starts[i]) for i in range(self.dimension)]
            else:
                patch_starts = [max_starts[i] // 2 for i in range(self.dimension)]

            slices = tuple(
                slice(start, start + size) for start, size in zip(patch_starts, self.patch_size)
            )
            data = data[slices]
            labels = labels[slices]

        data = torch.from_numpy(data).type(torch.float32)
        labels = torch.from_numpy(labels).type(torch.long)
        mask = self.compute_border_mask(init_shape, patch_starts)
        # if mask is not None:
        # mask = torch.from_numpy(mask).type(torch.float32)

        data = data.unsqueeze(0)  # Add channel dimension

        return data, labels, mask

    def compute_border_mask(self, init_shape, patch_starts):
        """
        Compute a mask to exclude the border of the patch in the loss.
        The model doesn't have full context to make a prediction there.

        Args:
            init_shape: shape of the original data
            patch_starts: starting indices of the patch in the original data

        Returns:
            bool tensor of the same shape as the original data, with False for the pixels to be masked
        """
        if not self.patch_size:
            return torch.full(init_shape, True)

        if not self.patch_border:
            return torch.full(self.patch_size, True)

        mask = torch.full(self.patch_size, False)
        slices = []
        for i in range(self.dimension):
            start = self.patch_border[i]
            if self.patch_border[i] > patch_starts[i]:
                # If the border wouldn't be taken into account by any other patch, keep it
                start -= patch_starts[i]
            end = self.patch_border[i]
            if self.patch_border[i] > (init_shape[i] - (patch_starts[i] + self.patch_size[i])):
                # If the border wouldn't be taken into account by any other patch, keep it
                end -= init_shape[i] - (patch_starts[i] + self.patch_size[i])
            end = self.patch_size[i] - end
            slices.append(slice(start, end))
            # slices.append((start, end))
        slices = tuple(slices)
        mask[slices] = True
        return mask


class XRayDataModule(L.LightningDataModule):
    """
    Lightning wrapper around training, validation, test dataloaders.
    Create train/validation/test split of data.

    Selections of samples and days are made for the training/validation sets, the remaining are
    used for the test set.
    A selection of heights is made across all datasets.

    The test set is split into two parts:
    - test_strict: no overlap in samples or days with training/validation sets
    - test_overlap: overlap in either samples or days with training/validation sets

    Args:
        hdf5_path: path to hdf5 file containing all data
        train_samples: list of sample names to use for training
        height_range: tuple of (min, max) height indices to use
        train_day_range: tuple of (min, max) day indices to use for training
        validation_split: fraction of training data to use for validation
        batch_size: batch size for dataloaders
        seed: random seed for splitting data
        num_workers: number of parallel workers for dataloaders
        dimension: number of spatial dimensions (2 or 3)
        patch_size: size of the patch to extract
        patch_border: size of the border of a patch to exclude from the loss
    """

    def __init__(
        self,
        hdf5_path: str,
        train_samples: List[str],
        height_range: Tuple[int, int],
        train_day_range: Tuple[int, int],
        validation_split: float,
        batch_size: int,
        seed: int,
        num_workers: int,
        dimension: int,
        patch_size: Optional[Union[int, Tuple[int, ...]]] = None,
        patch_border: Optional[Union[int, Tuple[int, ...]]] = None,
    ):
        super().__init__()
        self.hdf5_path = hdf5_path
        self.train_samples = train_samples
        self.height_range = height_range
        self.train_day_range = train_day_range
        self.validation_split = validation_split
        self.seed = seed
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.dimension = dimension
        patch_size = (patch_size,) * dimension if isinstance(patch_size, int) else patch_size
        patch_border = (
            (patch_border,) * dimension if isinstance(patch_border, int) else patch_border
        )
        self.dataset_kwargs = {
            'hdf5_path': hdf5_path,
            'patch_size': patch_size,
            'patch_border': patch_border,
            'dimension': dimension,
        }

        self.dataloaders = {}

    def prepare_data(self):
        datasets = {}
        train_val_selection = DataSelection(
            sample_list=self.train_samples,
            height_range=self.height_range,
            day_range=self.train_day_range,
            dimension=self.dimension,
        )
        train_val_dataset = XRayDataset(
            data_selection=train_val_selection, name='train_val', **self.dataset_kwargs
        )

        # split train/val randomly
        num_val_samples = int(self.validation_split * len(train_val_dataset))
        num_train_samples = len(train_val_dataset) - num_val_samples
        generator = torch.Generator().manual_seed(self.seed)
        datasets['train'], datasets['val'] = random_split(
            train_val_dataset, [num_train_samples, num_val_samples], generator=generator
        )
        datasets['train'].name = 'train'
        datasets['val'].name = 'val'

        # find test set by removing train/val samples
        with h5py.File(self.hdf5_path, 'r') as hdf5_file:
            all_samples = get_all_group_paths(hdf5_file)
            test_samples = list(set(all_samples) - set(self.train_samples))

            total_days = hdf5_file[all_samples[0]]['data'].shape[0]
            # Note: assumes training days start from 0
            test_day_range = (self.train_day_range[1], total_days)

        # The test set that has no overlaps in either samples or days
        strict_test_selection = DataSelection(
            sample_list=test_samples,
            height_range=self.height_range,
            day_range=test_day_range,
            dimension=self.dimension,
        )
        datasets['test_strict'] = XRayDataset(
            data_selection=strict_test_selection, name='test_strict', **self.dataset_kwargs
        )

        # The test set that has overlaps in either samples or days
        overlap_test_selection_same_days = DataSelection(
            sample_list=test_samples,
            height_range=self.height_range,
            day_range=self.train_day_range,
            dimension=self.dimension,
        )
        overlap_test_dataset_same_days = XRayDataset(
            data_selection=overlap_test_selection_same_days,
            name='test_overlap_same_days',
            **self.dataset_kwargs,
        )
        overlap_test_selection_same_samples = DataSelection(
            sample_list=self.train_samples,
            height_range=self.height_range,
            day_range=test_day_range,
            dimension=self.dimension,
        )
        overlap_test_dataset_same_samples = XRayDataset(
            data_selection=overlap_test_selection_same_samples,
            name='test_overlap_same_samples',
            **self.dataset_kwargs,
        )

        datasets['test_overlap'] = ConcatDataset(
            [overlap_test_dataset_same_days, overlap_test_dataset_same_samples]
        )
        datasets['test_overlap'].name = 'test_overlap'

        # turn into dataloaders
        self.dataloaders = {
            name: DataLoader(
                dataset,
                batch_size=self.batch_size,
                shuffle=(name == 'train'),
                num_workers=self.num_workers,
                persistent_workers=True,
            )
            for name, dataset in datasets.items()
        }

    def train_dataloader(self):
        return self.dataloaders['train']

    def val_dataloader(self):
        return self.dataloaders['val']

    def test_dataloader(self):
        return self.dataloaders['test_strict']

    def test_overlap_dataloader(self):
        return self.dataloaders['test_overlap']


def get_all_group_paths(hdf5_file):
    leaf_group_paths = []

    def check_leaf_group(name):
        item = hdf5_file[name]
        if isinstance(item, h5py.Group):
            # Check if it has datasets as children
            if any(isinstance(item[obj_name], h5py.Dataset) for obj_name in item):
                leaf_group_paths.append(name)

    hdf5_file.visit(check_leaf_group)
    return leaf_group_paths
