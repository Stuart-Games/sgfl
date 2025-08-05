import os
import subprocess
import requests
from dotenv import load_dotenv
from sys import platform
from pathlib import Path

load_dotenv()
cwd = Path.cwd()
PLACE_FILE_PATH = str(cwd) + "/WaterPhysics.rbxlx"
#pull and build
subprocess.run(["git", "pull"])
subprocess.run(["lune", "run","tools/build"])

#environment variables
placeId = os.getenv("PLACE_ID")
universeId = os.getenv("UNIVERSE_ID")
publishKey = os.getenv("PUBLISH_KEY")

#make correct publish req to roblox
url = f'https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published'
headers = {"x-api-key":publishKey,"Content-Type":"application/xml"}

f = open(PLACE_FILE_PATH,'rb') 
bin = f.read()
f.close()

res = requests.post(url,headers=headers,data=bin)

#open studio (generic window)
if platform == "win32": #windows (any ver)
    subprocess.run(["start", "roblox-studio:1"], shell=True)
elif platform == "darwin": #macos
    subprocess.run(["open", "roblox-studio:1"])

subprocess.run(["code", "."], shell=True)
subprocess.run(["rojo", "serve"])