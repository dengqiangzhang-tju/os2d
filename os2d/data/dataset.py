import os
import sys
import random
from collections import OrderedDict
import math
import copy
import logging
import pickle
import numpy as np
import pandas as pd

import torch
import torch.utils.data as data
import torchvision.transforms as transforms
from os2d.structures.bounding_box import BoxList

from PIL import Image

from os2d.engine.augmentation import DataAugmentation
from os2d.utils import get_image_size_after_resize_preserving_aspect_ratio, mkdir, read_image
from os2d.structures.feature_map import FeatureMapSize


def read_annotation_file(path):
    dataframe = pd.read_csv(path)

    # add "imagefilename" and "classfilename" columns with default file names
    if not "imagefilename" in dataframe.columns:
        imagefilename = []
        for row in dataframe["imageid"]:
            imagefilename.append(str(row)+".jpg")
        dataframe["imagefilename"] = imagefilename

    if not "classfilename" in dataframe.columns:
        classfilename = []
        for row in dataframe["classid"]:
            classfilename.append(str(row)+".jpg")
        dataframe["classfilename"] = classfilename

    required_columns = {"imageid", "imagefilename", "classid", "classfilename", "gtbboxid", "difficult", "lx", "ty", "rx", "by"}
    assert required_columns.issubset(dataframe.columns), "Missing columns in gtboxframe: {}".format(required_columns - set(dataframe.columns))

    return dataframe


def build_eval_dataset(data_path, name, eval_scale=None, cache_images=False, no_image_reading=False, logger_prefix="OS2D"):
    logger = logging.getLogger(f"{logger_prefix}.dataset")
    logger.info("Preparing the {0} dataset: eval scale {1}, image caching {2}".format(name, eval_scale, cache_images))
    
    if name.lower() == "dairy":
        annotation_folder="classes"
        image_size = 3000
        classdatafile = os.path.join(data_path, "dairy", annotation_folder,"dairy.csv")
        gt_path = os.path.join(data_path, "dairy", annotation_folder, "images")
        image_path = os.path.join(data_path, "dairy", "src", "original")
        gtboxframe = read_annotation_file(classdatafile)
    elif name.lower() in  ["paste-v", "paste-f"]:
        annotation_folder="classes"
        image_size = 1280
        classdatafile = os.path.join(data_path, "paste", annotation_folder,"paste.csv")
        gtboxframe = read_annotation_file(classdatafile)
        if name.lower() == "paste-f":
            gtboxframe["difficult"] = 0
        gt_path = os.path.join(data_path, "paste", annotation_folder, "images")
        image_path = os.path.join(data_path, "paste", "src", "original")
    else:
        raise(RuntimeError("Unknown dataset {0}".format(name)))

    dataset = DatasetOneShotDetection(gtboxframe, gt_path, image_path, name, image_size, eval_scale,
                                      cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)
    return dataset


def build_grozi_dataset(data_path, name, eval_scale=None, cache_images=False, no_image_reading=False, logger_prefix="OS2D"):
    logger = logging.getLogger(f"{logger_prefix}.dataset")
    logger.info("Preparing the GroZi-3.2k dataset: version {0}, eval scale {1}, image caching {2}".format(name, eval_scale, cache_images))

    annotation_folder="classes"
    image_size = 3264
    classdatafile = os.path.join(data_path, "grozi", annotation_folder,"grozi.csv")
    gt_path = os.path.join(data_path, "grozi", annotation_folder, "images")
    image_path = os.path.join(data_path, "grozi", "src", str(image_size))
    gtboxframe = read_annotation_file(classdatafile)

    # define a subset split (using closure)
    subset_name = name.lower()
    assert subset_name.startswith("grozi"), ""
    subset_name = subset_name[len("grozi"):]
    subsets = ["train", "val-old-cl", "val-new-cl", "val-all", "train-mini"]
    found_subset = False
    for subset in subsets:
        if subset_name == "-"+subset:
            found_subset = subset
            break
    assert found_subset, "Could not identify subset {}".format(subset_name)

    def get_unique_images(gtboxframe):
        unique_images = gtboxframe[["imageid", "imagefilename"]].drop_duplicates()
        image_ids = list(unique_images["imageid"])
        image_file_names = list(unique_images["imagefilename"])
        return image_ids, image_file_names

    if subset in ["train", "train-mini"]:
        gtboxframe = gtboxframe[gtboxframe["split"] == "train"]
        image_ids, image_file_names = get_unique_images(gtboxframe)
        if subset == "train-mini":
            image_ids = image_ids[:2]
            image_file_names = image_file_names[:2]
            gtboxframe = gtboxframe[gtboxframe["imageid"].isin(image_ids)]
    elif subset in ["val-old-cl", "val-new-cl", "val-all"]:
        gtboxframe = gtboxframe[gtboxframe["split"].isin(["val-old-cl", "val-new-cl"])]
        image_ids, image_file_names = get_unique_images(gtboxframe)
        if subset != "val-all":
            gtboxframe = gtboxframe[gtboxframe["split"] == subset]
    else:
        raise RuntimeError("Unknown subset {0}".format(subset))

    dataset = DatasetOneShotDetection(gtboxframe, gt_path, image_path, name, image_size, eval_scale,
                                      image_ids=image_ids, image_file_names=image_file_names,
                                      cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)
    return dataset


def build_instre_dataset(data_path, name, eval_scale=None, cache_images=False, no_image_reading=False, logger_prefix="OS2D"):
    logger = logging.getLogger(f"{logger_prefix}.dataset")
    logger.info("Preparing the INSTRE dataset: version {0}, eval scale {1}, image caching {2}".format(name, eval_scale, cache_images))
    # INSTRE dataset was downloaded from here: ftp://ftp.irisa.fr/local/texmex/corpus/instre/instre.tar.gz
    # Splits by Iscen et al. (2016) were downloaded from here: ftp://ftp.irisa.fr/local/texmex/corpus/instre/gnd_instre.mat

    image_size = 1000
    import scipy.io as sio
    dataset_path = os.path.join(data_path, "instre")
    annotation_file = os.path.join(dataset_path, "gnd_instre.mat")
    annotation_data = sio.loadmat(annotation_file)
    # annotation_data["qimlist"][0] - 1250 queries - each in annotation_data["qimlist"][0][i][0] file, root - os.path.join(data_path, "instre")
    # annotation_data["imlist"][0] - 27293 database images - each in annotation_data["imlist"][0][i][0] file, root - os.path.join(data_path, "instre")
    # annotation_data["gnd"][0] - 1250 annotations for all queries:
    #   annotation_data["gnd"][0][i][0] - indices of positives in annotation_data["imlist"][0] (WARNING - 1-based)
    #   annotation_data["gnd"][0][i][1] - bbox of the query object, one of the boxes from ent of *.txt
    #   images in subsets INSTRE-S1 and INSTRE-S2 contain exactly one object
    #   images in the subset INSTRE-M contain two objects each

    image_path = dataset_path
    gt_path = os.path.join(dataset_path, "classes")
    gt_image_path = os.path.join(gt_path, "images")
    mkdir(gt_image_path)

    classdatafile = os.path.join(gt_path, "instre.csv")
    if not os.path.isfile(classdatafile):
        logger.info(f"Did not find data file {classdatafile}, creating it from INSTRE source data")
        # create the annotation file from the raw dataset
        annotation_data["qimlist"] = annotation_data["qimlist"].flatten()
        annotation_data["imlist"] = annotation_data["imlist"].flatten()
        annotation_data["gnd"] = annotation_data["gnd"].flatten()
        num_classes = len(annotation_data["qimlist"])
        gtboxframe = [] # will be creating dataframe from a list of dicts
        for i_class in range(num_classes):
            query_image_path_original = str(annotation_data["qimlist"][i_class][0])
            if query_image_path_original.split("/")[0].lower() == "instre-m":
                # Query boxes from subset "INSTRE-M" contain both objects, so it is not clear how to use them
                logger.info(f"Skipping query {i_class}: {query_image_path_original}")
                continue
            logger.info(f"Adding query {i_class}: {query_image_path_original}")
            query_bbox = annotation_data["gnd"][i_class][1].flatten()
            query_positives = annotation_data["gnd"][i_class][0].flatten() - 1 # "-1" because of the original MATLAB indexing

            classid = i_class
            classfilename = f"{i_class:05d}_{'_'.join(query_image_path_original.split('/'))}"

            if not os.path.isfile(classfilename):
                query_img = read_image(os.path.join(dataset_path, query_image_path_original))
                query_img_cropped_box = query_img.crop(query_bbox)
                query_img_cropped_box.save(os.path.join(gt_image_path, classfilename))

            def convert_the_box_from_xywh(box, imsize):
                lx = float(box[0]) / imsize.w
                ty = float(box[1]) / imsize.h
                rx = lx + float(box[2]) / imsize.w
                by = ty + float(box[3]) / imsize.h
                return lx, ty, rx, by

            def read_boxes_from(file_with_boxes):
                with open(file_with_boxes, "r") as fo:
                    lines = fo.readlines()
                boxes = [[int(s) for s in line.split(" ")] for line in lines if line]
                return boxes

            def get_box_file_for_image_file(image_filename):
                return image_filename.split(".")[0] + ".txt"

            def get_the_boxes(image_filename):
                file_with_boxes = os.path.join(image_path, get_box_file_for_image_file(image_filename))
                # get image size - recompute boxes
                boxes = read_boxes_from(file_with_boxes)
                img = read_image(os.path.join(image_path, image_filename))
                imsize = FeatureMapSize(img=img)
                # choose the correct box if have two of them
                # From INSTRE documentation:
                # Specially, for each tuple-class in INSTRE-M, there are two corresponding object classes in INSTRE-S1.
                # In each annotation file for a INSTRE-M image, the first line records the object labeled as [a] in INSTRE-S1
                # and the second line records the object labeled as [b] in INSTRE-S1.
                #
                # CAUTION! the matlab file has boxes in x1, y1, x2, y2, but the .txt files in x, y, w, h
                query_path_split = query_image_path_original.split("/")
                image_filename_split = image_filename.split("/")
                if query_path_split[0].lower() == "instre-s1" and image_filename_split[0].lower() == "instre-m":
                    assert len(boxes) == 2, f"INSTRE-M images should have exactly two boxes, but have {boxes}"
                    assert query_path_split[1][2] in ["a", "b"]
                    i_box = 0 if query_path_split[1][2] == "a" else 1
                    boxes = [convert_the_box_from_xywh(boxes[i_box], imsize)]
                elif query_path_split[0].lower() == "instre-s1" and image_filename_split[0].lower() == "instre-s1" or \
                        query_path_split[0].lower() == "instre-s2" and image_filename_split[0].lower() == "instre-s2":
                        boxes = [convert_the_box_from_xywh(box, imsize) for box in boxes]
                else:
                    raise RuntimeError(f"Should not be happening, query {query_image_path_original}, image {image_filename}, boxes {boxes}")
                return boxes

            for image_id in query_positives:
                # add one bbox to the annotation
                #     required_columns = ["imageid", "imagefilename", "classid", "classfilename", "gtbboxid", "difficult", "lx", "ty", "rx", "by"]
                image_file_name = str(annotation_data["imlist"][image_id][0])
                boxes = get_the_boxes(image_file_name)
                for box in boxes:
                    item = OrderedDict()
                    item["gtbboxid"] = len(gtboxframe)
                    item["classid"] = classid
                    item["classfilename"] = classfilename
                    item["imageid"] = image_id
                    assert annotation_data["imlist"][image_id].size == 1
                    item["imagefilename"] = image_file_name
                    item["difficult"] = 0
                    item["lx"], item["ty"], item["rx"], item["by"] = box
                    gtboxframe.append(item)

        gtboxframe = pd.DataFrame(gtboxframe)
        gtboxframe.to_csv(classdatafile)

    gtboxframe = read_annotation_file(classdatafile)

    # get these automatically from gtboxframe
    image_ids = None
    image_file_names = None

    # define a subset split (using closure)
    subset_name = name.lower()
    assert subset_name.startswith("instre"), ""
    subset_name = subset_name[len("instre"):]
    subsets = ["all", "s1-train", "s1-val", "s1-test", "s2-train", "s2-val", "s2-test"]
    found_subset = False
    for subset in subsets:
        if subset_name == "-"+subset:
            found_subset = subset
            break
    assert found_subset, "Could not identify subset {}".format(subset_name)

    if subset == "all":
        pass
    elif subset in ["s1-train", "s1-val", "s1-test"]:
        gtboxframe = gtboxframe[gtboxframe.classfilename.str.contains("INSTRE-S1")]
        classes = gtboxframe.classfilename.drop_duplicates()
        if subset == "s1-train":
            classes = classes[:len(classes) * 75 // 100] # first 75%
        elif subset == "s1-test":
            classes = classes[len(classes) * 8 // 10:] # last 20%
        else: # "s1-val"
            classes = classes[len(classes) * 75 // 100 : len(classes) * 8 // 10] # 5%
        gtboxframe = gtboxframe[gtboxframe.classfilename.isin(classes)]
    elif subset in ["s2-train", "s2-val", "s2-test"]:
        gtboxframe = gtboxframe[gtboxframe.classfilename.str.contains("INSTRE-S2")]
        classes = gtboxframe.classfilename.drop_duplicates()
        if subset == "s2-train":
            classes = classes[:len(classes) * 75 // 100] # first 75%
        elif subset == "s2-test":
            classes = classes[len(classes) * 8 // 10:] # last 20%
        else: # "s2-val"
            classes = classes[len(classes) * 75 // 100 : len(classes) * 8 // 10] # 5%
        gtboxframe = gtboxframe[gtboxframe.classfilename.isin(classes)]
    else:
        raise(RuntimeError("Unknown subset {0}".format(subset)))

    dataset = DatasetOneShotDetection(gtboxframe, gt_image_path, image_path, name, image_size, eval_scale,
                                      image_ids=image_ids, image_file_names=image_file_names,
                                      cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)
    return dataset


def build_dataset_by_name(data_path, name, eval_scale=None, cache_images=False, no_image_reading=False, logger_prefix="OS2D"):
    if name.lower().startswith("grozi"):
        return build_grozi_dataset(data_path, name, eval_scale, cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)
    elif name.lower().startswith("instre"):
        return build_instre_dataset(data_path, name, eval_scale, cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)
    else:
        return build_eval_dataset(data_path, name, eval_scale, cache_images=cache_images, no_image_reading=no_image_reading, logger_prefix=logger_prefix)


class DatasetOneShotDetection(data.Dataset):
    """Dataset to load images/labels/boxes from a dataframe.
    """
    def __init__(self, gtboxframe, gt_path, image_path, name, image_size, eval_scale,
                       cache_images=False, no_image_reading=False,
                       image_ids=None, image_file_names=None, logger_prefix="OS2D"):
        self.logger = logging.getLogger(f"{logger_prefix}.dataset")
        self.name = name
        self.image_size = image_size
        self.eval_scale = eval_scale
        self.cache_images = cache_images
        
        self.gtboxframe = gtboxframe
        required_columns = {"imageid", "imagefilename", "classid", "classfilename", "gtbboxid", "difficult", "lx", "ty", "rx", "by"}
        assert required_columns.issubset(self.gtboxframe.columns), "Missing columns in gtboxframe: {}".format(required_columns - set(self.gtboxframe.columns))

        self.gt_path = gt_path
        self.image_path = image_path
        self.have_images_read = False

        if image_ids is not None and image_file_names is not None:
            self.image_ids = image_ids
            self.image_file_names = image_file_names
        else:
            unique_images = gtboxframe[["imageid", "imagefilename"]].drop_duplicates()
            self.image_ids = list(unique_images["imageid"])
            self.image_file_names = list(unique_images["imagefilename"])

        if not no_image_reading:
            # read GT images
            self._read_dataset_gt_images()
            # read data images
            self._read_dataset_images()
            self.have_images_read=True

        self.num_images = len(self.image_ids)
        self.num_boxes = len(self.gtboxframe)
        self.num_classes = len(self.gtboxframe["classfilename"].unique())
        
        self.logger.info("Loaded dataset {0} with {1} images, {2} boxes, {3} classes".format(
            self.name, self.num_images, self.num_boxes, self.num_classes
        ))

    def get_name(self):
        return self.name

    def get_eval_scale(self):
        return self.eval_scale

    def get_class_ids(self):
        return self.gtboxframe["classid"].unique()

    def get_class_ids_for_image_ids(self, image_ids):
        dataframe = self.get_dataframe_for_image_ids(image_ids)
        return dataframe["classid"].unique()

    def get_dataframe_for_image_ids(self, image_ids):
        return self.gtboxframe[self.gtboxframe["imageid"].isin(image_ids)]

    def get_image_size_for_image_id(self, image_id):
        return self.image_size_per_image_id[image_id]
    
    def _read_dataset_images(self):
        # create caches
        self.image_path_per_image_id = OrderedDict()
        self.image_size_per_image_id = OrderedDict()
        self.image_per_image_id = OrderedDict()
        for image_id, image_file in zip(self.image_ids, self.image_file_names):
            if image_id not in self.image_path_per_image_id :
                # store the image path
                img_path = os.path.join(self.image_path, image_file)
                self.image_path_per_image_id[image_id] = img_path
                # get image size (needed for bucketing)
                img = self._get_dataset_image_by_id(image_id)
                self.image_size_per_image_id[image_id] = FeatureMapSize(img=img)

        self.logger.info("{1} {0} data images".format(len(self.image_path_per_image_id), "Read" if self.cache_images else "Found"))

    def _read_dataset_gt_images(self):
        self.gt_images_per_classid = OrderedDict()
        for index, row in self.gtboxframe.iterrows():
            gt_file = row["classfilename"]
            class_id = row["classid"]
            if class_id not in self.gt_images_per_classid:
                # if the GT image is not read save it to the dataset
                self.gt_images_per_classid[class_id] = read_image(os.path.join(self.gt_path, gt_file))
        self.logger.info("Read {0} GT images".format(len(self.gt_images_per_classid)))

    def split_images_into_buckets_by_size(self):
        buckets = []
        bucket_image_size = []
        for image_id, s in self.image_size_per_image_id.items():
            if s not in bucket_image_size:
                # create a new empty bucket
                bucket_image_size.append(s)
                buckets.append([])
            # add item to the suitable bucket
            i_bucket = bucket_image_size.index(s)
            buckets[i_bucket].append(image_id)
        return buckets

    def _get_dataset_image_by_id(self, image_id):
        assert image_id in self.image_path_per_image_id, "Can work only with checked images"

        if image_id not in self.image_per_image_id :
            img_path = self.image_path_per_image_id[image_id]
            img = read_image(img_path)
            img_size = FeatureMapSize(img=img)
            if max(img_size.w, img_size.h) != self.image_size:
                h, w = get_image_size_after_resize_preserving_aspect_ratio(img_size.h, img_size.w, self.image_size)
                img = img.resize((w, h), resample=Image.ANTIALIAS) # resize images in case they were not of the correct size on disk
            if self.cache_images:
                self.image_per_image_id[image_id] = img
        else:
            img = self.image_per_image_id[image_id]

        return img

    @staticmethod
    def get_boxes_from_image_dataframe(image_data, image_size):
        if not image_data.empty:
            # get the labels
            label_ids_global = torch.tensor(list(image_data["classid"]), dtype=torch.long)
            difficult_flag = torch.tensor(list(image_data["difficult"] == 1), dtype=torch.bool)

            # get the boxes
            boxes = image_data[["lx", "ty", "rx", "by"]].to_numpy()
            # renorm boxes using the image size
            boxes[:, 0] *= image_size.w
            boxes[:, 2] *= image_size.w
            boxes[:, 1] *= image_size.h
            boxes[:, 3] *= image_size.h
            boxes = torch.FloatTensor(boxes)

            boxes = BoxList(boxes, image_size=image_size, mode="xyxy")
        else:
            boxes = BoxList.create_empty(image_size)
            label_ids_global = torch.tensor([], dtype=torch.long)
            difficult_flag = torch.tensor([], dtype=torch.bool)

        boxes.add_field("labels", label_ids_global)
        boxes.add_field("difficult", difficult_flag)
        boxes.add_field("labels_original", label_ids_global)
        boxes.add_field("difficult_original", difficult_flag)
        return boxes

    def get_image_annotation_for_imageid(self, image_id):
        # get data for this image
        image_data = self.gtboxframe[self.gtboxframe["imageid"] == image_id]
        img_size = self.image_size_per_image_id[image_id]
        boxes = self.get_boxes_from_image_dataframe(image_data, img_size)
        return boxes

    def copy_subset(self, subset_size=None, set_eval_mode=True):
        dataset_subset = copy.copy(self)  # shallow copy

        if subset_size is not None:
            dataset_subset.num_images = min(subset_size, dataset_subset.num_images)
            dataset_subset.image_ids = self.image_ids[:dataset_subset.num_images]
            dataset_subset.image_file_names = self.image_file_names[:dataset_subset.num_images]
            image_mask = dataset_subset.gtboxframe["imageid"].isin(dataset_subset.image_ids)
            dataset_subset.gtboxframe = dataset_subset.gtboxframe[image_mask]

            dataset_subset.name = self.name + "-subset{}".format(subset_size)

            # reload data
            dataset_subset._read_dataset_gt_images()
            dataset_subset._read_dataset_images()

        if set_eval_mode:
            # turn off data augmentation
            dataset_subset.data_augmentation = None

        return dataset_subset
