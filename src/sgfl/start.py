import subprocess
import requests
from sys import platform
import util


PLACE_FILE_PATH = util.getFileURI("WaterPhysics")
#pull and build
subprocess.run(["git", "pull"])
subprocess.run(["lune", "run","tools/build"])

#environment variables
placeId = util.getEnvSafe("PUBLISH_KEY")
universeId = util.getEnvSafe("UNIVERSE_ID")
publishKey = util.getEnvSafe("PUBLISH_KEY")

#make correct publish req to roblox
url = f'https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published'
headers:dict[str,str] = {"x-api-key":publishKey,"Content-Type":"application/xml"}

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