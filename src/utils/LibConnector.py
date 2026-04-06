from datetime import datetime
from comtypes import GUID
from comtypes.client import CreateObject

import pygetwindow as gw
import winreg as reg
import tkinter as tk
import win32process
import subprocess
import pyautogui
import pyperclip
import keyboard
import win32gui
import comtypes
import ctypes
import psutil
import time
import sys
import os
import re


class LibConnector:
    def __init__(self) -> None:
        self.datetime = datetime
        self.GUID = GUID
        self.CreateObject = CreateObject
        self.gw = gw
        self.reg = reg
        self.tk = tk
        self.win32process = win32process
        self.subprocess = subprocess
        self.pyautogui = pyautogui
        self.pyperclip = pyperclip
        self.keyboard = keyboard
        self.win32gui = win32gui
        self.comtypes = comtypes
        self.ctypes = ctypes
        self.psutil = psutil
        self.time = time
        self.sys = sys
        self.os = os
        self.re = re
        return
