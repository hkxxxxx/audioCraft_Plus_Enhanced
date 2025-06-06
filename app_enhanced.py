# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Updated to account for UI changes from https://github.com/rkfg/audiocraft/blob/long/app.py
# also released under the MIT license.

import argparse
from concurrent.futures import ProcessPoolExecutor
import os
from pathlib import Path
import subprocess as sp
from tempfile import NamedTemporaryFile
import time
import warnings
import glob
import re
from PIL import Image
from pydub import AudioSegment
from datetime import datetime

import json
import shutil
import taglib
import torch
import torchaudio
import gradio as gr
import numpy as np
import typing as tp

from audiocraft.data.audio_utils import convert_audio
from audiocraft.data.audio import audio_write
from audiocraft.models import AudioGen, MusicGen, MultiBandDiffusion
from audiocraft.utils import ui
import random, string

version = "2.0.1"

theme = gr.themes.Base(
    primary_hue="lime",
    secondary_hue="lime",
    neutral_hue="neutral",
).set(
    button_primary_background_fill_hover='*primary_500',
    button_primary_background_fill_hover_dark='*primary_500',
    button_secondary_background_fill_hover='*primary_500',
    button_secondary_background_fill_hover_dark='*primary_500'
)

MODEL = None  # Last used model
MODELS = None
UNLOAD_MODEL = False
MOVE_TO_CPU = False
IS_BATCHED = "facebook/MusicGen" in os.environ.get('SPACE_ID', '')
print(IS_BATCHED)
MAX_BATCH_SIZE = 12
BATCHED_DURATION = 15
INTERRUPTING = False
MBD = None
# We have to wrap subprocess call to clean a bit the log when using gr.make_waveform
_old_call = sp.call


def generate_random_string(length):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


def resize_video(input_path, output_path, target_width, target_height):
    ffmpeg_cmd = [
        'ffmpeg',
        '-y',
        '-i', input_path,
        '-vf', f'scale={target_width}:{target_height}',
        '-c:a', 'copy',
        output_path
    ]
    sp.run(ffmpeg_cmd)


def _call_nostderr(*args, **kwargs):
    # Avoid ffmpeg vomiting on the logs.
    kwargs['stderr'] = sp.DEVNULL
    kwargs['stdout'] = sp.DEVNULL
    _old_call(*args, **kwargs)


sp.call = _call_nostderr
# Preallocating the pool of processes.
pool = ProcessPoolExecutor(4)
pool.__enter__()


def interrupt():
    global INTERRUPTING
    INTERRUPTING = True


class FileCleaner:
    def __init__(self, file_lifetime: float = 3600):
        self.file_lifetime = file_lifetime
        self.files = []

    def add(self, path: tp.Union[str, Path]):
        self._cleanup()
        self.files.append((time.time(), Path(path)))

    def _cleanup(self):
        now = time.time()
        for time_added, path in list(self.files):
            if now - time_added > self.file_lifetime:
                if path.exists():
                    path.unlink()
                self.files.pop(0)
            else:
                break


file_cleaner = FileCleaner()


def make_waveform(*args, **kwargs):
    # Further remove some warnings.
    be = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        height = kwargs.pop('height')
        width = kwargs.pop('width')
        if height < 256:
            height = 256
        if width < 256:
            width = 256
        waveform_video = gr.make_waveform(*args, **kwargs)
        out = f"{generate_random_string(12)}.mp4"
        image = kwargs.get('bg_image', None)
        if image is None:
            resize_video(waveform_video, out, 900, 300)
        else:
            resize_video(waveform_video, out, width, height)
        print("Make a video took", time.time() - be)
        return out


def load_model(version='GrandaddyShmax/musicgen-melody', custom_model=None, gen_type="music"):
    global MODEL, MODELS
    print("Loading model", version)
    if MODELS is None:
        if version == 'GrandaddyShmax/musicgen-custom':
            MODEL = MusicGen.get_pretrained(custom_model)
        else:
            if gen_type == "music":
                MODEL = MusicGen.get_pretrained(version)
            elif gen_type == "audio":
                MODEL = AudioGen.get_pretrained(version)

        return

    else:
        t1 = time.monotonic()
        if MODEL is not None:
            MODEL.to('cpu') # move to cache
            print("Previous model moved to CPU in %.2fs" % (time.monotonic() - t1))
            t1 = time.monotonic()
        if version != 'GrandaddyShmax/musicgen-custom' and MODELS.get(version) is None:
            print("Loading model %s from disk" % version)
            if gen_type == "music":
                result = MusicGen.get_pretrained(version)
            elif gen_type == "audio":
                result = AudioGen.get_pretrained(version)
            MODELS[version] = result
            print("Model loaded in %.2fs" % (time.monotonic() - t1))
            MODEL = result
            return
        result = MODELS[version].to('cuda')
        print("Cached model loaded in %.2fs" % (time.monotonic() - t1))
        MODEL = result

def get_audio_info(audio_path):
    if audio_path is not None:
        if audio_path.name.endswith(".wav") or audio_path.name.endswith(".mp4") or audio_path.name.endswith(".json"):
            if not audio_path.name.endswith(".json"):
                with taglib.File(audio_path.name, save_on_exit=False) as song:
                    if 'COMMENT' not in song.tags:
                        return "No tags found. Either the file is not generated by MusicGen+ V1.2.7 and higher or the tags are corrupted. (Discord removes metadata from mp4 and wav files, so you can't use them)"
                    json_string = song.tags['COMMENT'][0]
                    data = json.loads(json_string)
                    global_prompt = str("\nGlobal Prompt: " + (data['global_prompt'] if data['global_prompt'] != "" else "none")) if 'global_prompt' in data else ""
                    bpm = str("\nBPM: " + data['bpm']) if 'bpm' in data else ""
                    key = str("\nKey: " + data['key']) if 'key' in data else ""
                    scale = str("\nScale: " + data['scale']) if 'scale' in data else ""
                    prompts = str("\nPrompts: " + (data['texts'] if data['texts'] != "['']" else "none")) if 'texts' in data else ""
                    duration = str("\nDuration: " + data['duration']) if 'duration' in data else ""
                    overlap = str("\nOverlap: " + data['overlap']) if 'overlap' in data else ""
                    seed = str("\nSeed: " + data['seed']) if 'seed' in data else ""
                    audio_mode = str("\nAudio Mode: " + data['audio_mode']) if 'audio_mode' in data else ""
                    input_length = str("\nInput Length: " + data['input_length']) if 'input_length' in data else ""
                    channel = str("\nChannel: " + data['channel']) if 'channel' in data else ""
                    sr_select = str("\nSample Rate: " + data['sr_select']) if 'sr_select' in data else ""
                    gen_type = str(data['generator'] + "gen-") if 'generator' in data else ""
                    model = str("\nModel: " + gen_type + data['model']) if 'model' in data else ""
                    custom_model = str("\nCustom Model: " + data['custom_model']) if 'custom_model' in data else ""
                    decoder = str("\nDecoder: " + data['decoder']) if 'decoder' in data else ""
                    topk = str("\nTopk: " + data['topk']) if 'topk' in data else ""
                    topp = str("\nTopp: " + data['topp']) if 'topp' in data else ""
                    temperature = str("\nTemperature: " + data['temperature']) if 'temperature' in data else ""
                    cfg_coef = str("\nClassifier Free Guidance: " + data['cfg_coef']) if 'cfg_coef' in data else ""
                    version = str("Version: " + data['version']) if 'version' in data else "Version: Unknown"
                    info = str(version + global_prompt + bpm + key + scale + prompts + duration + overlap + seed + audio_mode + input_length + channel + sr_select + model + custom_model + decoder + topk + topp + temperature + cfg_coef)
                    if info == "":
                        return "No tags found. Either the file is not generated by MusicGen+ V1.2.7 and higher or the tags are corrupted. (Discord removes metadata from mp4 and wav files, so you can't use them)"
                    return info
            else:
                with open(audio_path.name) as json_file:
                    data = json.load(json_file)
                    #if 'global_prompt' not in data:
                        #return "No tags found. Either the file is not generated by MusicGen+ V1.2.8a and higher or the tags are corrupted."
                    global_prompt = str("\nGlobal Prompt: " + (data['global_prompt'] if data['global_prompt'] != "" else "none")) if 'global_prompt' in data else ""
                    bpm = str("\nBPM: " + data['bpm']) if 'bpm' in data else ""
                    key = str("\nKey: " + data['key']) if 'key' in data else ""
                    scale = str("\nScale: " + data['scale']) if 'scale' in data else ""
                    prompts = str("\nPrompts: " + (data['texts'] if data['texts'] != "['']" else "none")) if 'texts' in data else ""
                    duration = str("\nDuration: " + data['duration']) if 'duration' in data else ""
                    overlap = str("\nOverlap: " + data['overlap']) if 'overlap' in data else ""
                    seed = str("\nSeed: " + data['seed']) if 'seed' in data else ""
                    audio_mode = str("\nAudio Mode: " + data['audio_mode']) if 'audio_mode' in data else ""
                    input_length = str("\nInput Length: " + data['input_length']) if 'input_length' in data else ""
                    channel = str("\nChannel: " + data['channel']) if 'channel' in data else ""
                    sr_select = str("\nSample Rate: " + data['sr_select']) if 'sr_select' in data else ""
                    gen_type = str(data['generator'] + "gen-") if 'generator' in data else ""
                    model = str("\nModel: " + gen_type + data['model']) if 'model' in data else ""
                    custom_model = str("\nCustom Model: " + data['custom_model']) if 'custom_model' in data else ""
                    decoder = str("\nDecoder: " + data['decoder']) if 'decoder' in data else ""
                    topk = str("\nTopk: " + data['topk']) if 'topk' in data else ""
                    topp = str("\nTopp: " + data['topp']) if 'topp' in data else ""
                    temperature = str("\nTemperature: " + data['temperature']) if 'temperature' in data else ""
                    cfg_coef = str("\nClassifier Free Guidance: " + data['cfg_coef']) if 'cfg_coef' in data else ""
                    version = str("Version: " + data['version']) if 'version' in data else "Version: Unknown"
                    info = str(version + global_prompt + bpm + key + scale + prompts + duration + overlap + seed + audio_mode + input_length + channel + sr_select + model + custom_model + decoder + topk + topp + temperature + cfg_coef)
                    if info == "":
                        return "No tags found. Either the file is not generated by MusicGen+ V1.2.7 and higher or the tags are corrupted."
                    return info
        else:
            return "Only .wav ,.mp4 and .json files are supported"
    else:
        return None


def info_to_params(audio_path):
    if audio_path is not None:
        if audio_path.name.endswith(".wav") or audio_path.name.endswith(".mp4") or audio_path.name.endswith(".json"):
            if not audio_path.name.endswith(".json"):
                with taglib.File(audio_path.name, save_on_exit=False) as song:
                    if 'COMMENT' not in song.tags:
                        return "Default", False, "", 120, "C", "Major", "large", None, 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, "sample", 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"
                    json_string = song.tags['COMMENT'][0]
                    data = json.loads(json_string)
                    struc_prompt = (False if data['bpm'] == "none" else True) if 'bpm' in data else False
                    global_prompt = data['global_prompt'] if 'global_prompt' in data else ""
                    bpm = (120 if data['bpm'] == "none" else int(data['bpm'])) if 'bpm' in data else 120
                    key = ("C" if data['key'] == "none" else data['key']) if 'key' in data else "C"
                    scale = ("Major" if data['scale'] == "none" else data['scale']) if 'scale' in data else "Major"
                    model = data['model'] if 'model' in data else "large"
                    custom_model = (data['custom_model'] if (data['custom_model']) in get_available_folders() else None) if 'custom_model' in data else None
                    decoder = data['decoder'] if 'decoder' in data else "Default"
                    if 'texts' not in data:
                        unique_prompts = 1
                        text = ["", "", "", "", "", "", "", "", "", ""]
                        repeat = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
                    else:
                        s = data['texts']
                        s = re.findall(r"'(.*?)'", s)
                        text = []
                        repeat = []
                        i = 0
                        for elem in s:
                            if elem.strip():
                                if i == 0 or elem != s[i-1]:
                                    text.append(elem)
                                    repeat.append(1)
                                else:
                                    repeat[-1] += 1
                            i += 1
                        text.extend([""] * (10 - len(text)))
                        repeat.extend([1] * (10 - len(repeat)))
                        unique_prompts = len([t for t in text if t])
                    audio_mode = ("sample" if data['audio_mode'] == "none" else data['audio_mode']) if 'audio_mode' in data else "sample"
                    duration = int(data['duration']) if 'duration' in data else 10
                    topk = float(data['topk']) if 'topk' in data else 250
                    topp = float(data['topp']) if 'topp' in data else 0
                    temperature = float(data['temperature']) if 'temperature' in data else 1.0
                    cfg_coef = float(data['cfg_coef']) if 'cfg_coef' in data else 5.0
                    seed = int(data['seed']) if 'seed' in data else -1
                    overlap = int(data['overlap']) if 'overlap' in data else 12
                    channel = data['channel'] if 'channel' in data else "stereo"
                    sr_select = data['sr_select'] if 'sr_select' in data else "48000"
                    return decoder, struc_prompt, global_prompt, bpm, key, scale, model, custom_model, unique_prompts, text[0], text[1], text[2], text[3], text[4], text[5], text[6], text[7], text[8], text[9], repeat[0], repeat[1], repeat[2], repeat[3], repeat[4], repeat[5], repeat[6], repeat[7], repeat[8], repeat[9], audio_mode, duration, topk, topp, temperature, cfg_coef, seed, overlap, channel, sr_select
            else:
                with open(audio_path.name) as json_file:
                    data = json.load(json_file)
                    struc_prompt = (False if data['bpm'] == "none" else True) if 'bpm' in data else False
                    global_prompt = data['global_prompt'] if 'global_prompt' in data else ""
                    bpm = (120 if data['bpm'] == "none" else int(data['bpm'])) if 'bpm' in data else 120
                    key = ("C" if data['key'] == "none" else data['key']) if 'key' in data else "C"
                    scale = ("Major" if data['scale'] == "none" else data['scale']) if 'scale' in data else "Major"
                    model = data['model'] if 'model' in data else "large"
                    custom_model = (data['custom_model'] if data['custom_model'] in get_available_folders() else None) if 'custom_model' in data else None
                    decoder = data['decoder'] if 'decoder' in data else "Default"
                    if 'texts' not in data:
                        unique_prompts = 1
                        text = ["", "", "", "", "", "", "", "", "", ""]
                        repeat = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
                    else:
                        s = data['texts']
                        s = re.findall(r"'(.*?)'", s)
                        text = []
                        repeat = []
                        i = 0
                        for elem in s:
                            if elem.strip():
                                if i == 0 or elem != s[i-1]:
                                    text.append(elem)
                                    repeat.append(1)
                                else:
                                    repeat[-1] += 1
                            i += 1
                        text.extend([""] * (10 - len(text)))
                        repeat.extend([1] * (10 - len(repeat)))
                        unique_prompts = len([t for t in text if t])
                    audio_mode = ("sample" if data['audio_mode'] == "none" else data['audio_mode']) if 'audio_mode' in data else "sample"
                    duration = int(data['duration']) if 'duration' in data else 10
                    topk = float(data['topk']) if 'topk' in data else 250
                    topp = float(data['topp']) if 'topp' in data else 0
                    temperature = float(data['temperature']) if 'temperature' in data else 1.0
                    cfg_coef = float(data['cfg_coef']) if 'cfg_coef' in data else 5.0
                    seed = int(data['seed']) if 'seed' in data else -1
                    overlap = int(data['overlap']) if 'overlap' in data else 12
                    channel = data['channel'] if 'channel' in data else "stereo"
                    sr_select = data['sr_select'] if 'sr_select' in data else "48000"
                    return decoder, struc_prompt, global_prompt, bpm, key, scale, model, custom_model, unique_prompts, text[0], text[1], text[2], text[3], text[4], text[5], text[6], text[7], text[8], text[9], repeat[0], repeat[1], repeat[2], repeat[3], repeat[4], repeat[5], repeat[6], repeat[7], repeat[8], repeat[9], audio_mode, duration, topk, topp, temperature, cfg_coef, seed, overlap, channel, sr_select
        else:
            return "Default", False, "", 120, "C", "Major", "large", None, 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, "sample", 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"
    else:
        return "Default", False, "", 120, "C", "Major", "large", None, 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, "sample", 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"


def info_to_params_a(audio_path):
    if audio_path is not None:
        if audio_path.name.endswith(".wav") or audio_path.name.endswith(".mp4") or audio_path.name.endswith(".json"):
            if not audio_path.name.endswith(".json"):
                with taglib.File(audio_path.name, save_on_exit=False) as song:
                    if 'COMMENT' not in song.tags:
                        return "Default", False, "", 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"
                    json_string = song.tags['COMMENT'][0]
                    data = json.loads(json_string)
                    struc_prompt = (False if data['global_prompt'] == "" else True) if 'global_prompt' in data else False
                    global_prompt = data['global_prompt'] if 'global_prompt' in data else ""
                    decoder = data['decoder'] if 'decoder' in data else "Default"
                    if 'texts' not in data:
                        unique_prompts = 1
                        text = ["", "", "", "", "", "", "", "", "", ""]
                        repeat = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
                    else:
                        s = data['texts']
                        s = re.findall(r"'(.*?)'", s)
                        text = []
                        repeat = []
                        i = 0
                        for elem in s:
                            if elem.strip():
                                if i == 0 or elem != s[i-1]:
                                    text.append(elem)
                                    repeat.append(1)
                                else:
                                    repeat[-1] += 1
                            i += 1
                        text.extend([""] * (10 - len(text)))
                        repeat.extend([1] * (10 - len(repeat)))
                        unique_prompts = len([t for t in text if t])
                    duration = int(data['duration']) if 'duration' in data else 10
                    topk = float(data['topk']) if 'topk' in data else 250
                    topp = float(data['topp']) if 'topp' in data else 0
                    temperature = float(data['temperature']) if 'temperature' in data else 1.0
                    cfg_coef = float(data['cfg_coef']) if 'cfg_coef' in data else 5.0
                    seed = int(data['seed']) if 'seed' in data else -1
                    overlap = int(data['overlap']) if 'overlap' in data else 12
                    channel = data['channel'] if 'channel' in data else "stereo"
                    sr_select = data['sr_select'] if 'sr_select' in data else "48000"
                    return decoder, struc_prompt, global_prompt, unique_prompts, text[0], text[1], text[2], text[3], text[4], text[5], text[6], text[7], text[8], text[9], repeat[0], repeat[1], repeat[2], repeat[3], repeat[4], repeat[5], repeat[6], repeat[7], repeat[8], repeat[9], duration, topk, topp, temperature, cfg_coef, seed, overlap, channel, sr_select
            else:
                with open(audio_path.name) as json_file:
                    data = json.load(json_file)
                    struc_prompt = (False if data['global_prompt'] == "" else True) if 'global_prompt' in data else False
                    global_prompt = data['global_prompt'] if 'global_prompt' in data else ""
                    decoder = data['decoder'] if 'decoder' in data else "Default"
                    if 'texts' not in data:
                        unique_prompts = 1
                        text = ["", "", "", "", "", "", "", "", "", ""]
                        repeat = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
                    else:
                        s = data['texts']
                        s = re.findall(r"'(.*?)'", s)
                        text = []
                        repeat = []
                        i = 0
                        for elem in s:
                            if elem.strip():
                                if i == 0 or elem != s[i-1]:
                                    text.append(elem)
                                    repeat.append(1)
                                else:
                                    repeat[-1] += 1
                            i += 1
                        text.extend([""] * (10 - len(text)))
                        repeat.extend([1] * (10 - len(repeat)))
                        unique_prompts = len([t for t in text if t])
                    duration = int(data['duration']) if 'duration' in data else 10
                    topk = float(data['topk']) if 'topk' in data else 250
                    topp = float(data['topp']) if 'topp' in data else 0
                    temperature = float(data['temperature']) if 'temperature' in data else 1.0
                    cfg_coef = float(data['cfg_coef']) if 'cfg_coef' in data else 5.0
                    seed = int(data['seed']) if 'seed' in data else -1
                    overlap = int(data['overlap']) if 'overlap' in data else 12
                    channel = data['channel'] if 'channel' in data else "stereo"
                    sr_select = data['sr_select'] if 'sr_select' in data else "48000"
                    return decoder, struc_prompt, global_prompt, unique_prompts, text[0], text[1], text[2], text[3], text[4], text[5], text[6], text[7], text[8], text[9], repeat[0], repeat[1], repeat[2], repeat[3], repeat[4], repeat[5], repeat[6], repeat[7], repeat[8], repeat[9], duration, topk, topp, temperature, cfg_coef, seed, overlap, channel, sr_select
                    
        else:
            return "Default", False, "", 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"
    else:
        return "Default", False, "", 1, "", "", "", "", "", "", "", "", "", "", 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 10, 250, 0, 1.0, 5.0, -1, 12, "stereo", "48000"


def make_pseudo_stereo (filename, sr_select, pan, delay):
    if pan:
        temp = AudioSegment.from_wav(filename)
        if sr_select != "32000":
            temp = temp.set_frame_rate(int(sr_select))
        left = temp.pan(-0.5) - 5
        right = temp.pan(0.6) - 5
        temp = left.overlay(right, position=5)
        temp.export(filename, format="wav")
    if delay:     
        waveform, sample_rate = torchaudio.load(filename) # load mono WAV file
        delay_seconds = 0.01 # set delay 10ms
        delay_samples = int(delay_seconds * sample_rate) # Calculating delay value in number of samples
        stereo_waveform = torch.stack([waveform[0], torch.cat((torch.zeros(delay_samples), waveform[0][:-delay_samples]))]) # Generate a stereo file with original mono audio and delayed version
        torchaudio.save(filename, stereo_waveform, sample_rate)
    return


def normalize_audio(audio_data):
    audio_data = audio_data.astype(np.float32)
    max_value = np.max(np.abs(audio_data))
    audio_data /= max_value
    return audio_data


def load_diffusion():
    global MBD
    if MBD is None:
        print("loading MBD")
        MBD = MultiBandDiffusion.get_mbd_musicgen()


def unload_diffusion():
    global MBD
    if MBD is not None:
        print("unloading MBD")
        MBD = None


def _do_predictions(gen_type, texts, melodies, sample, trim_start, trim_end, duration, image, height, width, background, bar1, bar2, channel, sr_select, progress=False, **gen_kwargs):
    if gen_type == "music":
        maximum_size = 29.5
    elif gen_type == "audio":
        maximum_size = 9.5
    cut_size = 0
    input_length = 0
    sampleP = None
    if sample is not None:
        globalSR, sampleM = sample[0], sample[1]
        sampleM = normalize_audio(sampleM)
        sampleM = torch.from_numpy(sampleM).t()
        if sampleM.dim() == 1:
            sampleM = sampleM.unsqueeze(0)
        sample_length = sampleM.shape[sampleM.dim() - 1] / globalSR
        if trim_start >= sample_length:
            trim_start = sample_length - 0.5
        if trim_end >= sample_length:
            trim_end = sample_length - 0.5
        if trim_start + trim_end >= sample_length:
            tmp = sample_length - 0.5
            trim_start = tmp / 2
            trim_end = tmp / 2
        sampleM = sampleM[..., int(globalSR * trim_start):int(globalSR * (sample_length - trim_end))]
        sample_length = sample_length - (trim_start + trim_end)
        if sample_length > maximum_size:
            cut_size = sample_length - maximum_size
            sampleP = sampleM[..., :int(globalSR * cut_size)]
            sampleM = sampleM[..., int(globalSR * cut_size):]
        if sample_length >= duration:
            duration = sample_length + 0.5
        input_length = sample_length
    global MODEL
    MODEL.set_generation_params(duration=(duration - cut_size), **gen_kwargs)
    print("new batch", len(texts), texts, [None if m is None else (m[0], m[1].shape) for m in melodies], [None if sample is None else (sample[0], sample[1].shape)])
    be = time.time()
    processed_melodies = []
    if gen_type == "music":
        target_sr = 32000
    elif gen_type == "audio":
        target_sr = 16000
    target_ac = 1

    for melody in melodies:
        if melody is None:
            processed_melodies.append(None)
        else:
            sr, melody = melody[0], torch.from_numpy(melody[1]).to(MODEL.device).float().t()
            if melody.dim() == 1:
                melody = melody[None]
            melody = melody[..., :int(sr * duration)]
            melody = convert_audio(melody, sr, target_sr, target_ac)
            processed_melodies.append(melody)

    if sample is not None:
        if sampleP is None:
            if gen_type == "music":
                outputs = MODEL.generate_continuation(
                    prompt=sampleM,
                    prompt_sample_rate=globalSR,
                    descriptions=texts,
                    progress=progress,
                    return_tokens=USE_DIFFUSION
                )
            elif gen_type == "audio":
                outputs = MODEL.generate_continuation(
                    prompt=sampleM,
                    prompt_sample_rate=globalSR,
                    descriptions=texts,
                    progress=progress
                )
        else:
            if sampleP.dim() > 1:
                sampleP = convert_audio(sampleP, globalSR, target_sr, target_ac)
            sampleP = sampleP.to(MODEL.device).float().unsqueeze(0)
            if gen_type == "music":
                outputs = MODEL.generate_continuation(
                    prompt=sampleM,
                    prompt_sample_rate=globalSR,
                    descriptions=texts,
                    progress=progress,
                    return_tokens=USE_DIFFUSION
                )
            elif gen_type == "audio":
                outputs = MODEL.generate_continuation(
                    prompt=sampleM,
                    prompt_sample_rate=globalSR,
                    descriptions=texts,
                    progress=progress
                )
            outputs = torch.cat([sampleP, outputs], 2)
            
    elif any(m is not None for m in processed_melodies):
        if gen_type == "music":
            outputs = MODEL.generate_with_chroma(
                descriptions=texts,
                melody_wavs=processed_melodies,
                melody_sample_rate=target_sr,
                progress=progress,
                return_tokens=USE_DIFFUSION
            )
        elif gen_type == "audio":
            outputs = MODEL.generate_with_chroma(
                descriptions=texts,
                melody_wavs=processed_melodies,
                melody_sample_rate=target_sr,
                progress=progress
            )
    else:
        if gen_type == "music":
            outputs = MODEL.generate(texts, progress=progress, return_tokens=USE_DIFFUSION)
        elif gen_type == "audio":
            outputs = MODEL.generate(texts, progress=progress)

    if USE_DIFFUSION:
        print("outputs: " + str(outputs))
        outputs_diffusion = MBD.tokens_to_wav(outputs[1])
        outputs = torch.cat([outputs[0], outputs_diffusion], dim=0)
    outputs = outputs.detach().cpu().float()
    backups = outputs
    if channel == "stereo":
        outputs = convert_audio(outputs, target_sr, int(sr_select), 2)
    elif channel == "mono" and sr_select != "32000":
        outputs = convert_audio(outputs, target_sr, int(sr_select), 1)
    out_files = []
    out_audios = []
    out_backup = []
    for output in outputs:
        with NamedTemporaryFile("wb", suffix=".wav", delete=False) as file:
            audio_write(
                file.name, output, (MODEL.sample_rate if channel == "stereo effect" else int(sr_select)), strategy="loudness",
                loudness_headroom_db=16, loudness_compressor=True, add_suffix=False)

            if channel == "stereo effect":
                make_pseudo_stereo(file.name, sr_select, pan=True, delay=True);

            out_files.append(pool.submit(make_waveform, file.name, bg_image=image, bg_color=background, bars_color=(bar1, bar2), fg_alpha=1.0, bar_count=75, height=height, width=width))
            out_audios.append(file.name)
            file_cleaner.add(file.name)
            print(f'wav: {file.name}')
    for backup in backups:
        with NamedTemporaryFile("wb", suffix=".wav", delete=False) as file:
            audio_write(
                file.name, backup, MODEL.sample_rate, strategy="loudness",
                loudness_headroom_db=16, loudness_compressor=True, add_suffix=False)
            out_backup.append(file.name)
            file_cleaner.add(file.name)
    res = [out_file.result() for out_file in out_files]
    res_audio = out_audios
    res_backup = out_backup
    for file in res:
        file_cleaner.add(file)
        print(f'video: {file}')
    print("batch finished", len(texts), time.time() - be)
    print("Tempfiles currently stored: ", len(file_cleaner.files))
    if MOVE_TO_CPU:
        MODEL.to('cpu')
    if UNLOAD_MODEL:
        MODEL = None
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()
    return res, res_audio, res_backup, input_length


def predict_batched(texts, melodies):
    max_text_length = 512
    texts = [text[:max_text_length] for text in texts]
    load_model('melody')
    res = _do_predictions(texts, melodies, BATCHED_DURATION)
    return res


def add_tags(filename, tags): 
    json_string = None

    data = {
        "global_prompt": tags[0],
        "bpm": tags[1],
        "key": tags[2],
        "scale": tags[3],
        "texts": tags[4],
        "duration": tags[5],
        "overlap": tags[6],
        "seed": tags[7],
        "audio_mode": tags[8],
        "input_length": tags[9],
        "channel": tags[10],
        "sr_select": tags[11],
        "model": tags[12],
        "custom_model": tags[13],
        "decoder": tags[14],
        "topk": tags[15],  
        "topp": tags[16],
        "temperature": tags[17],
        "cfg_coef": tags[18],
        "generator": tags[19],
        "version": version
        }

    json_string = json.dumps(data)

    if os.path.exists(filename):
        with taglib.File(filename, save_on_exit=True) as song:
            song.tags = {'COMMENT': json_string }

    json_file = open(tags[7] + '.json', 'w')
    json_file.write(json_string)
    json_file.close()

    return json_file.name;


def save_outputs(mp4, wav_tmp, tags, gen_type):
    # mp4: .mp4 file name in root running folder of app.py    
    # wav_tmp: temporary wav file located in %TEMP% folder
    # seed - used seed 
    # exanple BgnJtr4Pn1AJ.mp4,  C:\Users\Alex\AppData\Local\Temp\tmp4ermrebs.wav,  195123182343465
    # procedure read generated .mp4 and wav files, rename it by using seed as name, 
    # and will store it to ./output/today_date/wav and  ./output/today_date/mp4 folders. 
    # if file with same seed number already exist its make postfix in name like seed(n) 
    # where is n - consiqunce number 1-2-3-4 and so on
    # then we store generated mp4 and wav into destination folders.     

    current_date = datetime.now().strftime("%Y%m%d")
    wav_directory = os.path.join(os.getcwd(), 'output', current_date, gen_type,'wav')
    mp4_directory = os.path.join(os.getcwd(), 'output', current_date, gen_type,'mp4')
    json_directory = os.path.join(os.getcwd(), 'output', current_date, gen_type,'json')
    os.makedirs(wav_directory, exist_ok=True)
    os.makedirs(mp4_directory, exist_ok=True)
    os.makedirs(json_directory, exist_ok=True)

    filename = str(tags[7]) + '.wav'
    target = os.path.join(wav_directory, filename)
    counter = 1
    while os.path.exists(target):
        filename = str(tags[7]) + f'({counter})' + '.wav'
        target = os.path.join(wav_directory, filename)
        counter += 1

    shutil.copyfile(wav_tmp, target); # make copy of original file
    json_file = add_tags(target, tags);
    
    wav_target=target;
    target=target.replace('wav', 'mp4');
    mp4_target=target;
    
    mp4=r'./' +mp4;    
    shutil.copyfile(mp4, target); # make copy of original file  
    _ = add_tags(target, tags);

    target=target.replace('mp4', 'json'); # change the extension to json
    json_target=target; # store the json target

    with open(target, 'w') as f: # open a writable file object
        shutil.copyfile(json_file, target); # make copy of original file
    
    os.remove(json_file)

    return wav_target, mp4_target, json_target;


def clear_cash():
    # delete all temporary files genegated my system
    current_date = datetime.now().date()
    current_directory = os.getcwd()
    files = glob.glob(os.path.join(current_directory, '*.mp4'))
    for file in files:
        creation_date = datetime.fromtimestamp(os.path.getctime(file)).date()
        if creation_date == current_date:
            os.remove(file)

    temp_directory = os.environ.get('TEMP')
    files = glob.glob(os.path.join(temp_directory, 'tmp*.mp4'))
    for file in files:
        creation_date = datetime.fromtimestamp(os.path.getctime(file)).date()
        if creation_date == current_date:
            os.remove(file)
   
    files = glob.glob(os.path.join(temp_directory, 'tmp*.wav'))
    for file in files:
        creation_date = datetime.fromtimestamp(os.path.getctime(file)).date()
        if creation_date == current_date:
            os.remove(file)

    files = glob.glob(os.path.join(temp_directory, 'tmp*.png'))
    for file in files:
        creation_date = datetime.fromtimestamp(os.path.getctime(file)).date()
        if creation_date == current_date:
            os.remove(file)
    return


def s2t(seconds, seconds2):
    # convert seconds to time format
    # seconds - time in seconds
    # return time in format 00:00
    m, s = divmod(seconds, 60)
    m2, s2 = divmod(seconds2, 60)
    if seconds != 0 and seconds < seconds2:
        s = s + 1
    return ("%02d:%02d - %02d:%02d" % (m, s, m2, s2))


def calc_time(gen_type, s, duration, overlap, d0, d1, d2, d3, d4, d5, d6, d7, d8, d9):
    # calculate the time of generation
    # overlap - overlap in seconds
    # d0-d9 - drag
    # return time in seconds
    d_amount = [int(d0), int(d1), int(d2), int(d3), int(d4), int(d5), int(d6), int(d7), int(d8), int(d9)]
    calc = []
    tracks = []
    time = 0
    s = s - 1
    max_time = duration
    max_limit = 0
    if gen_type == "music":
        max_limit = 30
    elif gen_type == "audio":
        max_limit = 10
    track_add = max_limit - overlap
    tracks.append(max_limit + ((d_amount[0] - 1) * track_add))
    for i in range(1, 10):
        tracks.append(d_amount[i] * track_add)
    
    if tracks[0] >= max_time or s == 0:
        calc.append(s2t(time, max_time))
        time = max_time
    else:
        calc.append(s2t(time, tracks[0]))
        time = tracks[0]

    for i in range(1, 10):
        if time + tracks[i] >= max_time or i == s:
            calc.append(s2t(time, max_time))
            time = max_time
        else:
            calc.append(s2t(time, time + tracks[i]))
            time = time + tracks[i]
    
    return calc[0], calc[1], calc[2], calc[3], calc[4], calc[5], calc[6], calc[7], calc[8], calc[9]


# Add at the top with other imports
from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler
import PIL


def generate_image_from_prompt(prompt, output_path=None, model_id = "sd-legacy/stable-diffusion-v1-5"):
    """Generate an image based on the prompt and save it to a temporary file."""
    print(f"Loading local image generation model {model_id}")
    
    # 使用本地模型路径
    try:
        pipe = StableDiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float16)
        pipe = pipe.to("cuda")
        
        # 生成图像
        image = pipe(prompt).images[0]
        
        if output_path is None:
            with NamedTemporaryFile("wb", suffix=".png", delete=False) as file:
                output_path = file.name
        
        image.save(output_path)
        file_cleaner.add(output_path)
        return output_path
    except Exception as e:
        print(f"Error loading model or generating image: {str(e)}")
        return None


# Modify the predict_full function to handle image generation
def predict_full(gen_type, model, decoder, custom_model, prompt_amount, struc_prompt, bpm, key, scale, global_prompt, p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, d0, d1, d2, d3, d4, d5, d6, d7, d8, d9, audio, mode, trim_start, trim_end, duration, topk, topp, temperature, cfg_coef, seed, overlap, image, height, width, background, bar1, bar2, channel, sr_select, instrument, generate_bg_image=False, progress=gr.Progress()):
    global INTERRUPTING
    global USE_DIFFUSION
    INTERRUPTING = False
    
    # Check if we need to generate an image
    if generate_bg_image and image is None:
        # Use the prompt for image generation
        prompt_to_use = global_prompt if global_prompt else p0
        if prompt_to_use:
            # Generate image from prompt
            image_path = generate_image_from_prompt(prompt_to_use)
            image = image_path

    if gen_type == "audio":
        custom_model = None
        custom_model_shrt = "none"
    elif gen_type == "music":
        custom_model_shrt = custom_model
        custom_model = "models/" + custom_model

    if temperature < 0:
        raise gr.Error("Temperature must be >= 0.")
    if topk < 0:
        raise gr.Error("Topk must be non-negative.")
    if topp < 0:
        raise gr.Error("Topp must be non-negative.")

    if trim_start < 0:
        trim_start = 0
    if trim_end < 0:
        trim_end = 0

    topk = int(topk)

    if decoder == "MultiBand_Diffusion":
        USE_DIFFUSION = True
        load_diffusion()
    else:
        USE_DIFFUSION = False
        unload_diffusion()

    if gen_type == "music":
        model_shrt = model
        model = "GrandaddyShmax/musicgen-" + model
    elif gen_type == "audio":
        model_shrt = model
        model = "GrandaddyShmax/audiogen-" + model

    if MODEL is None or MODEL.name != (model):
        load_model(model, custom_model, gen_type)
    else:
        if MOVE_TO_CPU:
            MODEL.to('cuda')

    if seed < 0:
        seed = random.randint(0, 0xffff_ffff_ffff)
    torch.manual_seed(seed)

    def _progress(generated, to_generate):
        progress((min(generated, to_generate), to_generate))
        if INTERRUPTING:
            raise gr.Error("Interrupted.")
    MODEL.set_custom_progress_callback(_progress)

    audio_mode = "none"
    melody = None
    sample = None
    if audio:
      audio_mode = mode
      if mode == "sample":
          sample = audio
      elif mode == "melody":
          melody = audio

    custom_model_shrt = "none" if model != "GrandaddyShmax/musicgen-custom" else custom_model_shrt

    text_cat = [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9]
    drag_cat = [d0, d1, d2, d3, d4, d5, d6, d7, d8, d9]
    texts = []
    raw_texts = []
    ind = 0
    ind2 = 0

    # 处理乐器提示
    instrument_text = ""
    if instrument != "无乐器":
        # 从乐器选项中提取英文部分
        if "(" in instrument and ")" in instrument:
            instrument_text = instrument.split("(")[1].split(")")[0]
        else:
            instrument_text = instrument

    while ind < prompt_amount:
        for ind2 in range(int(drag_cat[ind])):
            if not struc_prompt:
                # 将乐器添加到提示词中
                prompt_with_instrument = text_cat[ind]
                if instrument_text and prompt_with_instrument:
                    prompt_with_instrument = f"{prompt_with_instrument}, {instrument_text}"
                texts.append(prompt_with_instrument)
                global_prompt = "none"
                bpm = "none"
                key = "none"
                scale = "none"
                raw_texts.append(text_cat[ind])
            else:
                if gen_type == "music":
                    bpm_str = str(bpm) + " bpm"
                    key_str = ", " + str(key) + " " + str(scale)
                    global_str = (", " + str(global_prompt)) if str(global_prompt) != "" else ""
                elif gen_type == "audio":
                    bpm_str = ""
                    key_str = ""
                    global_str = (str(global_prompt)) if str(global_prompt) != "" else ""
                
                # 将乐器添加到提示词中
                prompt_text = str(text_cat[ind])
                if instrument_text and prompt_text:
                    prompt_text = f"{prompt_text}, {instrument_text}"
                    
                texts_str = (", " + prompt_text) if prompt_text != "" else ""
                texts.append(bpm_str + key_str + global_str + texts_str)
                raw_texts.append(text_cat[ind])
        ind2 = 0
        ind = ind + 1


    outs, outs_audio, outs_backup, input_length = _do_predictions(
        gen_type, [texts], [melody], sample, trim_start, trim_end, duration, image, height, width, background, bar1, bar2, channel, sr_select, progress=True,
        top_k=topk, top_p=topp, temperature=temperature, cfg_coef=cfg_coef, extend_stride=MODEL.max_duration-overlap)
    tags = [str(global_prompt), str(bpm), str(key), str(scale), str(raw_texts), str(duration), str(overlap), str(seed), str(audio_mode), str(input_length), str(channel), str(sr_select), str(model_shrt), str(custom_model_shrt), str(decoder), str(topk), str(topp), str(temperature), str(cfg_coef), str(gen_type)]
    wav_target, mp4_target, json_target = save_outputs(outs[0], outs_audio[0], tags, gen_type);
    # Removes the temporary files.
    for out in outs:
        os.remove(out)
    for out in outs_audio:
        os.remove(out)

    return mp4_target, wav_target, outs_backup[0], [mp4_target, wav_target, json_target], seed


max_textboxes = 10


#def get_available_models():
    #return sorted([re.sub('.pt$', '', item.name) for item in list(Path('models/').glob('*')) if item.name.endswith('.pt')])


def get_available_folders():
    models_dir = "models"
    folders = [f for f in os.listdir(models_dir) if os.path.isdir(os.path.join(models_dir, f))]
    return sorted(folders)


def toggle_audio_src(choice):
    if choice == "mic":
        return gr.update(source="microphone", value=None, label="Microphone")
    else:
        return gr.update(source="upload", value=None, label="File")


def ui_full(launch_kwargs):
    with gr.Blocks(title='基于AI辅助的音乐编辑创作工具的设计与实现', theme=theme) as interface:
        gr.Markdown(
            """
            # 基于AI辅助的音乐编辑创作工具的设计与实现

            ### 特别感谢: facebookresearch, Camenduru, rkfg, oobabooga, AlexHK and GrandaddyShmax
            """
        )
        with gr.Tab("音乐制作"):
            gr.Markdown(
                """
                ### MusicGen
                """
            )
            with gr.Row():
                with gr.Column():
                    with gr.Tab("制作"):
                        with gr.Accordion("结构提示", open=False):
                            with gr.Column():
                                with gr.Row():
                                    struc_prompts = gr.Checkbox(label="启用", value=False, interactive=True, container=False)
                                    bpm = gr.Number(label="节拍", value=120, interactive=True, scale=1, precision=0)
                                    key = gr.Dropdown(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "Bb", "B"], label="音调", value="C", interactive=True)
                                    scale = gr.Dropdown(["Major", "Minor"], label="音阶", value="Major", interactive=True)
                                with gr.Row():
                                    global_prompt = gr.Text(label="全局提示词", interactive=True, scale=3)
                        with gr.Row():
                            s = gr.Slider(1, max_textboxes, value=1, step=1, label="提示词:", interactive=True, scale=2)
                            #s_mode = gr.Radio(["segmentation", "batch"], value="segmentation", interactive=True, scale=1, label="Generation Mode")
                        # 添加乐器选择下拉菜单
                        with gr.Row():
                            instrument = gr.Dropdown(
                                ["无乐器", "钢琴(piano)", "吉他(guitar)", "电吉他(electric guitar)", "木吉他(acoustic guitar)", 
                                "小提琴(violin)", "萨克斯风(saxophone)", "合成器(synthesizer)", "鼓(drums)", "贝斯(bass)", "竖琴(harp)"], 
                                label="选择乐器", value="无乐器", interactive=True, scale=2
                            )
                        with gr.Column():
                            textboxes = []
                            prompts = []
                            repeats = []
                            calcs = []
                            with gr.Row():
                                text0 = gr.Text(label="输入文本", interactive=True, scale=4)
                                prompts.append(text0)
                                drag0 = gr.Number(label="重复", value=1, interactive=True, scale=1)
                                repeats.append(drag0)
                                calc0 = gr.Text(interactive=False, value="00:00 - 00:00", scale=1, label="Time")
                                calcs.append(calc0)
                            for i in range(max_textboxes):
                                with gr.Row(visible=False) as t:
                                    text = gr.Text(label="输入文本", interactive=True, scale=3)
                                    repeat = gr.Number(label="重复", minimum=1, value=1, interactive=True, scale=1)
                                    calc = gr.Text(interactive=False, value="00:00 - 00:00", scale=1, label="Time")
                                textboxes.append(t)
                                prompts.append(text)
                                repeats.append(repeat)
                                calcs.append(calc)
                            to_calc = gr.Button("Calculate Timings", variant="secondary")
                        with gr.Row():
                            duration = gr.Slider(minimum=1, maximum=300, value=10, step=1, label="持续时间", interactive=True)
                        with gr.Row():
                            overlap = gr.Slider(minimum=1, maximum=29, value=12, step=1, label="重叠", interactive=True)
                        with gr.Row():
                            seed = gr.Number(label="Seed", value=-1, scale=4, precision=0, interactive=True)
                            gr.Button('\U0001f3b2\ufe0f', scale=1).click(fn=lambda: -1, outputs=[seed], queue=False)
                            reuse_seed = gr.Button('\u267b\ufe0f', scale=1)

                    with gr.Tab("音频"):
                        with gr.Row():
                            with gr.Column():
                                input_type = gr.Radio(["file", "mic"], value="file", label="输入类型（可选）", interactive=True)
                                mode = gr.Radio(["melody", "sample"], label="输入音频模式（可选）", value="sample", interactive=True)
                                with gr.Row():
                                    trim_start = gr.Number(label="剪切开始", value=0, interactive=True)
                                    trim_end = gr.Number(label="剪切结束", value=0, interactive=True)
                            audio = gr.Audio(source="upload", type="numpy", label="输入音频（可选）", interactive=True)

                    with gr.Tab("定制"):
                        with gr.Row():
                            with gr.Column():
                                background = gr.ColorPicker(value="#0f0f0f", label="背景颜色", interactive=True, scale=0)
                                bar1 = gr.ColorPicker(value="#84cc16", label="条形图颜色开始", interactive=True, scale=0)
                                bar2 = gr.ColorPicker(value="#10b981", label="条形图颜色结束", interactive=True, scale=0)
                            with gr.Column():
                                image = gr.Image(label="背景图像", type="filepath", interactive=True, scale=4)
                                generate_bg_image = gr.Checkbox(label="根据提示生成背景图像", value=False)
                                with gr.Row():
                                    height = gr.Number(label="Height", value=512, interactive=True)
                                    width = gr.Number(label="Width", value=768, interactive=True)

                    with gr.Tab("设置"):
                        with gr.Row():
                            channel = gr.Radio(["mono", "stereo", "stereo effect"], label="输出音频通道", value="stereo", interactive=True, scale=1)
                            sr_select = gr.Dropdown(["11025", "16000", "22050", "24000", "32000", "44100", "48000"], label="输出音频采样率", value="48000", interactive=True)
                        with gr.Row():
                            model = gr.Radio(["melody", "small", "medium", "large", "custom"], label="模型", value="large", interactive=True, scale=1)
                            with gr.Column():
                                dropdown = gr.Dropdown(choices=get_available_folders(), value=("No models found" if len(get_available_folders()) < 1 else get_available_folders()[0]), label='自定义模型（模型文件夹）', elem_classes='slim-dropdown', interactive=True)
                                ui.create_refresh_button(dropdown, lambda: None, lambda: {'choices': get_available_folders()}, 'refresh-button')
                        with gr.Row():
                            decoder = gr.Radio(["Default", "MultiBand_Diffusion"], label="解码器", value="Default", interactive=True)
                        with gr.Row():
                            topk = gr.Number(label="Top-k", value=250, interactive=True)
                            topp = gr.Number(label="Top-p", value=0, interactive=True)
                            temperature = gr.Number(label="温度", value=1.0, interactive=True)
                            cfg_coef = gr.Number(label="无分类器引导", value=3.0, interactive=True)
                    with gr.Row():
                        submit = gr.Button("Generate", variant="primary")
                        # Adapted from https://github.com/rkfg/audiocraft/blob/long/app.py, MIT license.
                        _ = gr.Button("Interrupt").click(fn=interrupt, queue=False)
                with gr.Column() as c:
                    with gr.Tab("输出"):
                        output = gr.Video(label="生成音乐", scale=0)
                        with gr.Row():
                            audio_only = gr.Audio(type="numpy", label="仅音频", interactive=False)
                            backup_only = gr.Audio(type="numpy", label="备份音频", interactive=False, visible=False)
                            send_audio = gr.Button("发送到输入音频")
                        seed_used = gr.Number(label='Seed used', value=-1, interactive=False)
                        download = gr.File(label="生成文件", interactive=False)
                    with gr.Tab("说明"):
                        gr.Markdown(
                            """
                            -**[生成（按钮）]：**
                            根据给定的设置和提示生成音乐。

                            -**[中断（按钮）]：**
                            尽快停止音乐生成，提供不完整的输出。

                            ---

                            ###生成选项卡：
                            ####结构提示：
                            此功能允许您设置全局提示，有助于减少重复提示
                            这将用于所有提示段。

                            -**[结构提示（复选框）]：**
                            启用/禁用结构提示功能。

                            -**[节拍（数字）]：**
                            生成音乐的每分钟节拍数。

                            - **[音调 (dropdown)]:**  
                            生成音乐的音调。
                            
                           -**[音阶（下拉）]：**
                            生成音乐的规模。
                            Major（大调）‌：Major音阶通常听起来更加明亮和欢快，广泛应用于许多欢快的歌曲和旋律中。
                            Minor（小调）‌：Minor音阶则给人一种更加忧郁或悲伤的感觉，常用于表达悲伤、忧郁或深情的旋律。

                            -**[全局提示（文本）]：**
                            在这里写下您希望用于所有提示段的提示。

                            #### Multi-Prompt: 
                            
                            此功能允许您控制音乐，为不同的时间段添加变化。
                            您最多有10个提示段。第一个提示总是30秒长，其他提示将是[30s-重叠]。
                            例如，如果重叠时间为10秒，则每个提示段将为20秒。

                            - **[Prompt Segments (数字)]:**  
                            Amount of unique prompt to generate throughout the music generation.

                            -**[提示/输入文本（提示词）]：**
                            在这里描述您希望模型生成的音乐。

                           -**[重复（数字）]：**
                            写下此提示将重复多少次（而不是在同一提示上浪费另一个提示段）。

                            -**[时间（文本）]：**
                            提示片段的时间。

                            - **[Calculate Timings (按钮)]:**  
                            计算提示段的计时。

                           -**[持续时间（数字）]：**
                            您希望生成的音乐持续多长时间（以秒为单位）。

                            -**[重叠（数字）]：**
                            每个新段将引用前一段的量（以秒为单位）。
                            例如，如果您选择20s：第一个片段之后的每个新片段都将引用前一个片段20s
                            并且只会产生10秒的新音乐。该模型只能处理30秒的音乐。
                            
                            -**【Seed（数字）】：**
                            您生成的音乐id。如果您希望生成完全相同的音乐，
                            按照精确的提示放置精确的种子
                            （通过这种方式，您还可以扩展生成的特定歌曲）。

                            -**[随机种子（按钮）]：**
                            给出“-1”作为种子，这被视为随机种子。

                            -**[复制上一个种子（按钮）]：**
                            从输出种子中复制种子（如果你不想手动操作）。

                            ---

                            ### 音频选项卡:

                            -**[输入类型（选择）]：**
                            `文件模式允许您上传音频文件作为输入
                            `麦克风模式允许您使用麦克风作为输入

                            -**[输入音频模式（选择）]：**
                            `旋律模式只适用于旋律模型：它为音乐生成提供了参考旋律的条件
                            `Sample模式适用于任何模型：它向模型提供音乐样本以生成其延续。

                            -**[修剪开始和修剪结束（数字）]：**
                            `Trim Start`设置您希望从一开始就对输入音频进行多少修剪
                            `修剪末端与上述相同，但从末端开始

                            -**[输入音频（音频文件）]：**
                            在此处输入您希望在“旋律”或“样本”模式下使用的音频。

                            ---

                            ### 自定义选项卡:

                            -**[背景颜色]：**
                            仅当您不上传图像时才有效。波形背景的颜色。

                            -**[条形图颜色开始（颜色）]：**
                            波形条的第一种颜色。

                            -**[条形图颜色结束（颜色）]：**
                            波形条的第二种颜色。

                            -**[背景图像（图片）]：**
                            您希望与波形一起附加到生成的视频中的背景图像。

                           -**[高度和宽度（数字）]：**
                            输出视频分辨率，仅适用于图像。
                            （最小高度和宽度为256）。
                            
                            ---

                            ### 设置选项卡:

                           -**[输出音频通道（选择）]：**
                            通过此功能，您可以选择输出音频所需的通道数量。
                            `mono是一种简单的单声道音频
                            `立体声是一种双声道音频，但听起来或多或少像单声道
                            `立体声效果这个也是双声道的，但使用技巧来模拟立体声音频。

                           -**[输出音频采样率（下拉菜单）]：**
                            输出音频采样率，模型默认值为32000。

                            - **[模型（选择）]:**  
                            在这里，您可以选择要使用的模型：
                            `melody` 该模型基于具有独特功能的媒介模型，允许您使用旋律调节
                            `small` 模型在300M参数上训练
                            `medium` 模型在1.5B参数上训练
                            `large` 模型在3.3B参数上训练
                            `custom` 模型运行您提供的自定义模型。

                            - **[custom（选择）]:**  
                            此下拉菜单将显示放置在“models”文件夹中的模型
                            您必须在模型选项中选择“自定义”才能使用它。

                           -**[刷新（按钮）]：**
                            刷新自定义模型的下拉列表。

                           -**[解码器（选择）]：**
                            在此处选择要使用的解码器：
                            `Default `是默认解码器
                            `MultiBand_Diffusion是一种使用扩散来生成音频的解码器。

                            - **[Top-k (number)]:**  
                            是用于文本生成模型（包括音乐生成模型）的参数。它决定了在生成过程的每个步骤中最有可能考虑的下一个令牌的数量。该模型根据预测的概率对所有可能的令牌进行排名，然后从排名列表中选择前k个令牌。然后，该模型从这组缩减的令牌中采样，以确定生成序列中的下一个令牌。k值越小，输出越集中和确定，而k值越大，生成的音乐就越多样化。

                            - **[Top-p (number)]:**  
                            也称为核抽样或概率抽样，是文本生成过程中用于选择标记的另一种方法。与 top-k 那样指定固定数字不同，top-p 考虑排名标记​​的累积概率分布。它选择最小的一组标记，这些标记的累积概率超过某个阈值（通常表示为 p）。然后，模型从该集合中抽样以选择下一个标记。这种方法确保生成的输出在多样性和连贯性之间保持平衡，因为它允许根据概率考虑不同数量的标记。
                            
                            - **[温度 (number)]:**  
                            是控制生成输出随机性的参数。它应用于采样过程中，其中较高的温度值会产生更随机和多样化的输出，而较低的温度值会产生更确定和更集中的输出。在音乐生成的背景下，较高的温度可以为生成的音乐带来更多的变化和创造力，但也可能导致作品的连贯性或结构性降低。另一方面，较低的温度可以产生更多重复和可预测的音乐。

                            - **[无分类器引导（数字）]:**  
                            指某些音乐生成模型中使用的一种技术，即训练单独的分类器网络来指导或控制生成的音乐。该分类器在标记数据上进行训练，以识别特定的音乐特征或风格。在生成过程中，分类器会评估生成器模型的输出，并鼓励生成器生成符合所需特征或风格的音乐。这种方法允许对生成的音乐进行更细粒度的控制，使用户能够指定他们希望模型捕获的某些属性。
                            """
                        )
        with gr.Tab("音频制作"):
            gr.Markdown(
                """
                ### AudioGen
                """
            )
            with gr.Row():
                with gr.Column():
                    with gr.Tab("制作"):
                        with gr.Accordion("结构提示", open=False):
                            with gr.Row():
                                struc_prompts_a = gr.Checkbox(label="启用", value=False, interactive=True, container=False)
                                global_prompt_a = gr.Text(label="全局提示", interactive=True, scale=3)
                        with gr.Row():
                            s_a = gr.Slider(1, max_textboxes, value=1, step=1, label="提示词:", interactive=True, scale=2)
                        # 添加乐器选择下拉菜单
                        with gr.Row():
                            instrument_a = gr.Dropdown(
                                ["无乐器", "钢琴(piano)", "吉他(guitar)", "电吉他(electric guitar)", "木吉他(acoustic guitar)", 
                                "小提琴(violin)", "萨克斯风(saxophone)", "合成器(synthesizer)", "鼓(drums)", "贝斯(bass)", "竖琴(harp)"], 
                                label="选择乐器", value="无乐器", interactive=True, scale=2
                            )
                        with gr.Column():
                            textboxes_a = []
                            prompts_a = []
                            repeats_a = []
                            calcs_a = []
                            with gr.Row():
                                text0_a = gr.Text(label="输入文本", interactive=True, scale=4)
                                prompts_a.append(text0_a)
                                drag0_a = gr.Number(label="重复", value=1, interactive=True, scale=1)
                                repeats_a.append(drag0_a)
                                calc0_a = gr.Text(interactive=False, value="00:00 - 00:00", scale=1, label="时间")
                                calcs_a.append(calc0_a)
                            for i in range(max_textboxes):
                                with gr.Row(visible=False) as t_a:
                                    text_a = gr.Text(label="输入文本", interactive=True, scale=3)
                                    repeat_a = gr.Number(label="重复", minimum=1, value=1, interactive=True, scale=1)
                                    calc_a = gr.Text(interactive=False, value="00:00 - 00:00", scale=1, label="时间")
                                textboxes_a.append(t_a)
                                prompts_a.append(text_a)
                                repeats_a.append(repeat_a)
                                calcs_a.append(calc_a)
                            to_calc_a = gr.Button("Calculate Timings", variant="secondary")
                        with gr.Row():
                            duration_a = gr.Slider(minimum=1, maximum=300, value=10, step=1, label="持续时间", interactive=True)
                        with gr.Row():
                            overlap_a = gr.Slider(minimum=1, maximum=9, value=2, step=1, label="重叠", interactive=True)
                        with gr.Row():
                            seed_a = gr.Number(label="Seed", value=-1, scale=4, precision=0, interactive=True)
                            gr.Button('\U0001f3b2\ufe0f', scale=1).click(fn=lambda: -1, outputs=[seed_a], queue=False)
                            reuse_seed_a = gr.Button('\u267b\ufe0f', scale=1)

                    with gr.Tab("音频"):
                        with gr.Row():
                            with gr.Column():
                                input_type_a = gr.Radio(["file", "mic"], value="file", label="输入类型（可选）", interactive=True)
                                mode_a = gr.Radio(["sample"], label="输入音频模式（可选）", value="sample", interactive=False, visible=False)
                                with gr.Row():
                                    trim_start_a = gr.Number(label="修剪开始", value=0, interactive=True)
                                    trim_end_a = gr.Number(label="修剪结束", value=0, interactive=True)
                            audio_a = gr.Audio(source="upload", type="numpy", label="输入音频（可选）", interactive=True)

                    with gr.Tab("定制"):
                        with gr.Row():
                            with gr.Column():
                                background_a = gr.ColorPicker(value="#0f0f0f", label="背景颜色", interactive=True, scale=0)
                                bar1_a = gr.ColorPicker(value="#84cc16", label="条形图颜色开始", interactive=True, scale=0)
                                bar2_a = gr.ColorPicker(value="#10b981", label="条形图颜色结束", interactive=True, scale=0)
                            with gr.Column():
                                image_a = gr.Image(label="背景图像", type="filepath", interactive=True, scale=4)
                                generate_bg_image_a = gr.Checkbox(label="根据提示生成背景图像", value=False)
                                with gr.Row():
                                    height_a = gr.Number(label="Height", value=512, interactive=True)
                                    width_a = gr.Number(label="Width", value=768, interactive=True)

                    with gr.Tab("设置"):
                        with gr.Row():
                            channel_a = gr.Radio(["mono", "stereo", "stereo effect"], label="输出音频通道", value="stereo", interactive=True, scale=1)
                            sr_select_a = gr.Dropdown(["11025", "16000", "22050", "24000", "32000", "44100", "48000"], label="输出音频采样率", value="48000", interactive=True)
                        with gr.Row():
                            model_a = gr.Radio(["medium"], label="模型", value="medium", interactive=False, visible=False)
                            decoder_a = gr.Radio(["Default"], label="解码器", value="Default", interactive=False, visible=False)
                        with gr.Row():
                            topk_a = gr.Number(label="Top-k", value=250, interactive=True)
                            topp_a = gr.Number(label="Top-p", value=0, interactive=True)
                            temperature_a = gr.Number(label="温度", value=1.0, interactive=True)
                            cfg_coef_a = gr.Number(label="无分类器引导", value=3.0, interactive=True)
                    with gr.Row():
                        submit_a = gr.Button("Generate", variant="primary")
                        _ = gr.Button("Interrupt").click(fn=interrupt, queue=False)
                with gr.Column():
                    with gr.Tab("输出"):
                        output_a = gr.Video(label="生成音频", scale=0)
                        with gr.Row():
                            audio_only_a = gr.Audio(type="numpy", label="仅音频", interactive=False)
                            backup_only_a = gr.Audio(type="numpy", label="备份音频", interactive=False, visible=False)
                            send_audio_a = gr.Button("Send to Input Audio")
                        seed_used_a = gr.Number(label='Seed used', value=-1, interactive=False)
                        download_a = gr.File(label="生成的文件", interactive=False)
                    with gr.Tab("说明"):
                        gr.Markdown(
                            """
                            -**[生成（按钮）]：**
                            根据给定的设置和提示生成音频。

                            -**[中断（按钮）]：**
                            尽快停止音频生成，提供不完整的输出。

                            ---

                            ### 生成选项卡:

                            #### 结构提示:

                            此功能允许您设置全局提示，有助于减少重复提示
                            这将用于所有提示段。

                            -**[结构提示（复选框）]：**
                            启用/禁用结构提示功能。

                            -**[全局提示（文本）]：**
                            在这里写下您希望用于所有提示段的提示。

                            #### Multi-Prompt: 
                            
                            此功能允许您控制音频，为不同的时间段添加变化。
                            您最多有10个提示段。第一个提示总是10秒长
                            其他提示将是[10s-重叠]。
                            例如，如果重叠为2s，则每个提示段将为8s。

                            - **[Prompt Segments (number)]:**  
                            Amount of unique prompt to generate throughout the audio generation.

                            -**[提示/输入文本（提示）]：**
                            在这里描述您希望模型生成的音频。

                            -**[重复（数字）]：**
                            写下此提示将重复多少次（而不是在同一提示上浪费另一个提示段）。

                            -**[时间（文本）]：**
                            提示片段的时间。

                            -**[计算计时（按钮）]：**
                            计算提示段的计时。

                            -**[持续时间（数量）]：**
                            您希望生成的音频持续多长时间（以秒为单位）。

                            -**[重叠（数字）]：**
                            每个新段将引用前一段的量（以秒为单位）。
                            例如，如果选择2s：第一个分段之后的每个新分段都将引用前一个分段2s
                            并且将仅生成8秒的新音频。该模型只能处理10秒的音乐。

                            -**【Seed（数量）】：**
                            您生成的音频id。如果您希望生成完全相同的音频，
                            按照精确的提示放置精确的种子
                            （通过这种方式，您还可以扩展生成的特定歌曲）。

                            -**[随机种子（按钮）]：**
                            给出“-1”作为种子，这被视为随机种子。

                            -**[复制上一个种子（按钮）]：**
                            从输出种子中复制种子（如果你不想手动操作）。

                            ---

                            ### 音频选项卡:

                            - **[输入类型（选择）]:**  
                            `File` 模式允许您上传音频文件作为输入 
                            `Mic` 模式允许您使用麦克风作为输入

                            -**[修剪开始和修剪结束（数字）]：**
                            Trim Start`设置您希望从一开始就对输入音频进行多少修剪
                            Trim end 与上述相同，但从末端开始

                           -**[输入音频（音频文件）]：**
                            在此处输入您要使用的音频。

                            ---

                            ### 自定义选项卡:

                            -**[背景颜色]：**
                            仅当您不上传图像时才有效。波形背景的颜色。

                            -**[条形图颜色开始（颜色）]：**
                            波形条的第一种颜色。

                            -**[条形图颜色结束（颜色）]：**
                            波形条的第二种颜色。

                            -**[背景图像（图片）]：**
                            您希望与波形一起附加到生成的视频中的背景图像。

                            -**[高度和宽度（数字）]：**
                            输出视频分辨率，仅适用于图像。
                            （最小高度和宽度为256）。
                            
                            ---

                            ### 设置选项卡:

                            -**[输出音频通道（选择）]：**
                            通过此功能，您可以选择输出音频所需的通道数量。
                            `mono` 是一个简单的单声道音频
                            `stereo` 是双声道音频，但听起来或多或少像单声道
                            `stereo effect` 这个也是双通道的，但使用技巧来模拟立体声音频。

                            -**[输出音频采样率（下拉菜单）]：**
                            输出音频采样率，模型默认值为32000。

                            - **[Top-k (number)]:**  
                            is a parameter used in text generation models, including music generation models. It determines the number of most likely next tokens to consider at each step of the generation process. The model ranks all possible tokens based on their predicted probabilities, and then selects the top-k tokens from the ranked list. The model then samples from this reduced set of tokens to determine the next token in the generated sequence. A smaller value of k results in a more focused and deterministic output, while a larger value of k allows for more diversity in the generated music.

                            - **[Top-p (number)]:**  
                            also known as nucleus sampling or probabilistic sampling, is another method used for token selection during text generation. Instead of specifying a fixed number like top-k, top-p considers the cumulative probability distribution of the ranked tokens. It selects the smallest possible set of tokens whose cumulative probability exceeds a certain threshold (usually denoted as p). The model then samples from this set to choose the next token. This approach ensures that the generated output maintains a balance between diversity and coherence, as it allows for a varying number of tokens to be considered based on their probabilities.
                            
                            - **[Temperature (number)]:**  
                            is a parameter that controls the randomness of the generated output. It is applied during the sampling process, where a higher temperature value results in more random and diverse outputs, while a lower temperature value leads to more deterministic and focused outputs. In the context of music generation, a higher temperature can introduce more variability and creativity into the generated music, but it may also lead to less coherent or structured compositions. On the other hand, a lower temperature can produce more repetitive and predictable music.

                            - **[Classifier Free Guidance (number)]:**  
                            refers to a technique used in some music generation models where a separate classifier network is trained to provide guidance or control over the generated music. This classifier is trained on labeled data to recognize specific musical characteristics or styles. During the generation process, the output of the generator model is evaluated by the classifier, and the generator is encouraged to produce music that aligns with the desired characteristics or style. This approach allows for more fine-grained control over the generated music, enabling users to specify certain attributes they want the model to capture.
                            """
                        )
        with gr.Tab("音频信息"):
            gr.Markdown(
                """
                ### Audio Info
                """
            )
            with gr.Row():
                with gr.Column():
                    in_audio = gr.File(type="file", label="输入任意音频", interactive=True)
                    with gr.Row():
                        send_gen = gr.Button("Send to MusicGen", variant="primary")
                        send_gen_a = gr.Button("Send to AudioGen", variant="primary")
                with gr.Column():
                    info = gr.Textbox(label="音频信息", lines=10, interactive=False)
        with gr.Tab("变更日志"):
            gr.Markdown(
                            """
                            ## Changelog:

                            ### v2.0.1

                            - Changed custom model loading to support the official trained models

                            - Additional changes from the main facebookresearch repo



                            ### v2.0.0a

                            - Forgot to move all the update to app.py from temp2.py... oops



                            ### v2.0.0

                            - Changed name from MusicGen+ to AudioCraft Plus
                            
                            - Complete overhaul of the repo "backend" with the latest changes from the main facebookresearch repo

                            - Added a new decoder: MultiBand_Diffusion

                            - Added AudioGen: a new tab for generating audio



                            ### v1.2.8c

                            - Implemented Reverse compatibility for audio info tab with previous versions



                            ### v1.2.8b

                            - Fixed the error when loading default models



                            ### v1.2.8a

                            - Adapted Audio info tab to work with the new structure prompts feature

                            - Now custom models actually work, make sure you select the correct base model



                            ### v1.2.8

                            - Now you will also recieve json file with metadata of generated audio

                            - Added error messages in Audio Info tab

                            - Added structure prompts: you can select bpm, key and global prompt for all prompts

                            - Added time display next to each prompt, can be calculated with "Calculate Timings" button



                            ### v1.2.7

                            - When sending generated audio to Input Audio, it will send a backup audio with default settings  
                            (best for continuos generation)

                            - Added Metadata to generated audio (Thanks to AlexHK ♥)

                            - Added Audio Info tab that will display the metadata of the input audio

                            - Added "send to Text2Audio" button in Audio Info tab

                            - Generated audio is now stored in the "output" folder (Thanks to AlexHK ♥)

                            - Added an output area with generated files and download buttons

                            - Enhanced Stereo effect (Thanks to AlexHK ♥)



                            ### v1.2.6

                            - Added option to generate in stereo (instead of only mono)

                            - Added dropdown for selecting output sample rate (model default is 32000)



                            ### v1.2.5a

                            - Added file cleaner (This comes from the main facebookresearch repo)

                            - Reorganized a little, moved audio to a seperate tab



                            ### v1.2.5

                            - Gave a unique lime theme to the webui
                            
                            - Added additional output for audio only

                            - Added button to send generated audio to Input Audio

                            - Added option to trim Input Audio



                            ### v1.2.4

                            - Added mic input (This comes from the main facebookresearch repo)



                            ### v1.2.3

                            - Added option to change video size to fit the image you upload



                            ### v1.2.2

                            - Added Wiki, Changelog and About tabs



                            ### v1.2.1

                            - Added tabs and organized the entire interface

                            - Added option to attach image to the output video

                            - Added option to load fine-tuned models (Yet to be tested)



                            ### v1.2.0

                            - Added Multi-Prompt



                            ### v1.1.3

                            - Added customization options for generated waveform



                            ### v1.1.2

                            - Removed sample length limit: now you can input audio of any length as music sample



                            ### v1.1.1

                            - Improved music sample audio quality when using music continuation



                            ### v1.1.0

                            - Rebuilt the repo on top of the latest structure of the main MusicGen repo
                            
                            - Improved Music continuation feature



                            ### v1.0.0 - Stable Version

                            - Added Music continuation
                            """
                        )
        with gr.Tab("关于"):
            gen_type = gr.Text(value="music", interactive=False, visible=False)
            gen_type_a = gr.Text(value="audio", interactive=False, visible=False)
            gr.Markdown(
                            """
                            This is your private demo for [MusicGen](https://github.com/facebookresearch/audiocraft), a simple and controllable model for music generation
                            presented at: ["Simple and Controllable Music Generation"](https://huggingface.co/papers/2306.05284)
                            
                            ## MusicGen+ is an extended version of the original MusicGen by facebookresearch. 
                            
                            ### Repo: https://github.com/GrandaddyShmax/audiocraft_plus/tree/plus

                            ---
                            
                            ### This project was possible thanks to:

                            #### GrandaddyShmax - https://github.com/GrandaddyShmax

                            #### Camenduru - https://github.com/camenduru

                            #### rkfg - https://github.com/rkfg

                            #### oobabooga - https://github.com/oobabooga
                            
                            #### AlexHK - https://github.com/alanhk147
                            """
                        )

        send_gen.click(info_to_params, inputs=[in_audio], outputs=[decoder, struc_prompts, global_prompt, bpm, key, scale, model, dropdown, s, prompts[0], prompts[1], prompts[2], prompts[3], prompts[4], prompts[5], prompts[6], prompts[7], prompts[8], prompts[9], repeats[0], repeats[1], repeats[2], repeats[3], repeats[4], repeats[5], repeats[6], repeats[7], repeats[8], repeats[9], mode, duration, topk, topp, temperature, cfg_coef, seed, overlap, channel, sr_select], queue=False)
        reuse_seed.click(fn=lambda x: x, inputs=[seed_used], outputs=[seed], queue=False)
        send_audio.click(fn=lambda x: x, inputs=[backup_only], outputs=[audio], queue=False)
        submit.click(predict_full, inputs=[gen_type, model, decoder, dropdown, s, struc_prompts, bpm, key, scale, global_prompt, prompts[0], prompts[1], prompts[2], prompts[3], prompts[4], prompts[5], prompts[6], prompts[7], prompts[8], prompts[9], repeats[0], repeats[1], repeats[2], repeats[3], repeats[4], repeats[5], repeats[6], repeats[7], repeats[8], repeats[9], audio, mode, trim_start, trim_end, duration, topk, topp, temperature, cfg_coef, seed, overlap, image, height, width, background, bar1, bar2, channel, sr_select, instrument, generate_bg_image], outputs=[output, audio_only, backup_only, download, seed_used])
        input_type.change(toggle_audio_src, input_type, [audio], queue=False, show_progress=False)
        to_calc.click(calc_time, inputs=[gen_type, s, duration, overlap, repeats[0], repeats[1], repeats[2], repeats[3], repeats[4], repeats[5], repeats[6], repeats[7], repeats[8], repeats[9]], outputs=[calcs[0], calcs[1], calcs[2], calcs[3], calcs[4], calcs[5], calcs[6], calcs[7], calcs[8], calcs[9]], queue=False)

        send_gen_a.click(info_to_params_a, inputs=[in_audio], outputs=[decoder_a, struc_prompts_a, global_prompt_a, s_a, prompts_a[0], prompts_a[1], prompts_a[2], prompts_a[3], prompts_a[4], prompts_a[5], prompts_a[6], prompts_a[7], prompts_a[8], prompts_a[9], repeats_a[0], repeats_a[1], repeats_a[2], repeats_a[3], repeats_a[4], repeats_a[5], repeats_a[6], repeats_a[7], repeats_a[8], repeats_a[9], duration_a, topk_a, topp_a, temperature_a, cfg_coef_a, seed_a, overlap_a, channel_a, sr_select_a], queue=False)
        reuse_seed_a.click(fn=lambda x: x, inputs=[seed_used_a], outputs=[seed_a], queue=False)
        send_audio_a.click(fn=lambda x: x, inputs=[backup_only_a], outputs=[audio_a], queue=False)
        submit_a.click(predict_full, inputs=[gen_type_a, model_a, decoder_a, dropdown, s_a, struc_prompts_a, bpm, key, scale, global_prompt_a, prompts_a[0], prompts_a[1], prompts_a[2], prompts_a[3], prompts_a[4], prompts_a[5], prompts_a[6], prompts_a[7], prompts_a[8], prompts_a[9], repeats_a[0], repeats_a[1], repeats_a[2], repeats_a[3], repeats_a[4], repeats_a[5], repeats_a[6], repeats_a[7], repeats_a[8], repeats_a[9], audio_a, mode_a, trim_start_a, trim_end_a, duration_a, topk_a, topp_a, temperature_a, cfg_coef_a, seed_a, overlap_a, image_a, height_a, width_a, background_a, bar1_a, bar2_a, channel_a, sr_select_a, instrument_a, generate_bg_image_a], outputs=[output_a, audio_only_a, backup_only_a, download_a, seed_used_a])
        input_type_a.change(toggle_audio_src, input_type_a, [audio_a], queue=False, show_progress=False)
        to_calc_a.click(calc_time, inputs=[gen_type_a, s_a, duration_a, overlap_a, repeats_a[0], repeats_a[1], repeats_a[2], repeats_a[3], repeats_a[4], repeats_a[5], repeats_a[6], repeats_a[7], repeats_a[8], repeats_a[9]], outputs=[calcs_a[0], calcs_a[1], calcs_a[2], calcs_a[3], calcs_a[4], calcs_a[5], calcs_a[6], calcs_a[7], calcs_a[8], calcs_a[9]], queue=False)

        in_audio.change(get_audio_info, in_audio, outputs=[info])

        def variable_outputs(k):
            k = int(k) - 1
            return [gr.Textbox.update(visible=True)]*k + [gr.Textbox.update(visible=False)]*(max_textboxes-k)
        def get_size(image):
            if image is not None:
                img = Image.open(image)
                img_height = img.height
                img_width = img.width
                if (img_height%2) != 0:
                    img_height = img_height + 1
                if (img_width%2) != 0:
                    img_width = img_width + 1
                return img_height, img_width
            else:
                return 512, 768

        image.change(get_size, image, outputs=[height, width])
        image_a.change(get_size, image_a, outputs=[height_a, width_a])
        s.change(variable_outputs, s, textboxes)
        s_a.change(variable_outputs, s_a, textboxes_a)
        interface.queue().launch(**launch_kwargs)


def ui_batched(launch_kwargs):
    with gr.Blocks() as demo:
        gr.Markdown(
            """
            # MusicGen

            This is the demo for [MusicGen](https://github.com/facebookresearch/audiocraft),
            a simple and controllable model for music generation
            presented at: ["Simple and Controllable Music Generation"](https://huggingface.co/papers/2306.05284).
            <br/>
            <a href="https://huggingface.co/spaces/facebook/MusicGen?duplicate=true"
                style="display: inline-block;margin-top: .5em;margin-right: .25em;" target="_blank">
            <img style="margin-bottom: 0em;display: inline;margin-top: -.25em;"
                src="https://bit.ly/3gLdBN6" alt="Duplicate Space"></a>
            for longer sequences, more control and no queue.</p>
            """
        )
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    text = gr.Text(label="描述你的音乐", lines=2, interactive=True)
                    with gr.Column():
                        radio = gr.Radio(["file", "mic"], value="file",
                                         label="旋律条件（可选）文件或麦克风")
                        melody = gr.Audio(source="upload", type="numpy", label="文件",
                                          interactive=True, elem_id="melody-input")
                with gr.Row():
                    submit = gr.Button("Generate")
            with gr.Column():
                output = gr.Video(label="生成音乐")
                audio_output = gr.Audio(label="生成音乐（wav）", type='filepath')
        submit.click(predict_batched, inputs=[text, melody],
                     outputs=[output, audio_output], batch=True, max_batch_size=MAX_BATCH_SIZE)
        radio.change(toggle_audio_src, radio, [melody], queue=False, show_progress=False)
        gr.Examples(
            fn=predict_batched,
            examples=[
                [
                    "An 80s driving pop song with heavy drums and synth pads in the background",
                    "./assets/bach.mp3",
                ],
                [
                    "A cheerful country song with acoustic guitars",
                    "./assets/bolero_ravel.mp3",
                ],
                [
                    "90s rock song with electric guitar and heavy drums",
                    None,
                ],
                [
                    "a light and cheerly EDM track, with syncopated drums, aery pads, and strong emotions bpm: 130",
                    "./assets/bach.mp3",
                ],
                [
                    "lofi slow bpm electro chill with organic samples",
                    None,
                ],
            ],
            inputs=[text, melody],
            outputs=[output]
        )
        gr.Markdown("""
        ### More details

        该模型将根据您提供的描述生成12秒的音频。
        您可以选择提供参考音频，从中提取宽广的旋律。
        
        然后，模型将尝试遵循提供的描述和旋律。
        所有样本都是用“旋律”模型生成的。
        """)

        demo.queue(max_size=8 * 4).launch(**launch_kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--listen',
        type=str,
        default='0.0.0.0' if 'SPACE_ID' in os.environ else '127.0.0.1',
        help='IP to listen on for connections to Gradio',
    )
    parser.add_argument(
        '--username', type=str, default='', help='Username for authentication'
    )
    parser.add_argument(
        '--password', type=str, default='', help='Password for authentication'
    )
    parser.add_argument(
        '--server_port',
        type=int,
        default=0,
        help='Port to run the server listener on',
    )
    parser.add_argument(
        '--inbrowser', action='store_true', help='Open in browser'
    )
    parser.add_argument(
        '--share', action='store_true', help='Share the gradio UI'
    )
    parser.add_argument(
        '--unload_model', action='store_true', help='Unload the model after every generation to save GPU memory'
    )

    parser.add_argument(
        '--unload_to_cpu', action='store_true', help='Move the model to main RAM after every generation to save GPU memory but reload faster than after full unload (see above)'
    )

    parser.add_argument(
        '--cache', action='store_true', help='Cache models in RAM to quickly switch between them'
    )

    args = parser.parse_args()
    UNLOAD_MODEL = args.unload_model
    MOVE_TO_CPU = args.unload_to_cpu
    if args.cache:
        MODELS = {}

    launch_kwargs = {}
    launch_kwargs['server_name'] = args.listen

    if args.username and args.password:
        launch_kwargs['auth'] = (args.username, args.password)
    if args.server_port:
        launch_kwargs['server_port'] = args.server_port
    if args.inbrowser:
        launch_kwargs['inbrowser'] = args.inbrowser
    if args.share:
        launch_kwargs['share'] = args.share

    # Show the interface
    if IS_BATCHED:
        global USE_DIFFUSION
        USE_DIFFUSION = False
        ui_batched(launch_kwargs)
    else:
        ui_full(launch_kwargs)