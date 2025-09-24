from pathlib import Path
import os
import json
import subprocess

def getFileURI(name: str)->str:
    cwd = Path.cwd()
    return str(cwd) + "/" + name

def getAbsoluteFileURI(name:str)->str:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(BASE_DIR,name)

def runLuauFile(name:str):
    fileURI = getAbsoluteFileURI(name)
    absolutePath = getAbsoluteFileURI("")
    subprocess.run(["lune","run",fileURI,absolutePath])

def runSilentSubprocess(arr:list[str]):
    subprocess.run(arr,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def getTableFromJsonFile(filePath:str):
    with open(getAbsoluteFileURI(filePath),'r') as f: 
        table = json.load(f)
        return table

def createDirIfNotExist(dirName:str):
    if not os.path.isdir(dirName):
        os.mkdir(dirName)

def getEnvSafe(key:str)->str:
    val = os.getenv(key)
    if val == None:
        raise Exception(f'Could not find {key} in root .env')
    return val

def deleteFile(path:str):
    if os.path.exists(path):
        os.remove(path)

PLACE_FILE_PATH = getFileURI("Place.rbxlx")
ASSET_CONFIG_FILE_PATH = getFileURI("assets.json")