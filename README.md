# Stuart Games Workflow Package (sgfl)

## Purpose

This package enables the building and saving of Stuart Games projects in a consistent and portable manner.

## Requirements

3. Python 3.9 or greater is required
1. A lune and a rojo installation is required either globally, or in the relevant directory.
1. The directory you are working with must have the correct env keys listed below.

```
USER_ID your user id with edit perms
PUBLISH_KEY the key used for publishing
DOWNLOAD_KEY the key used for generating a download link from roblox
PLACE_ID the id of the place we are trying to publish
UNIVERSE_ID the id of the experience we are trying to pubish
```

An `assets.json` file is now required in the root directory to specify the saving of assets.

Example structure:

```
{
    "Materials": {
        "folder":"assets",
        "robloxPath":"MaterialService"
    },
    "ServerSignals": {
        "folder":"assets",
        "robloxPath":"ServerStorage.Signals"
    }
}
```

NB: There is support for direct service read/write and subfolder read/write but not both for the same service. In the example above you can see we save a specific folder in ServerStorage but all of MaterialService. We would not, in this case, be able to save all of ServerStorage to one file lest we have duplication in the file system.

## Installation

run the command `pipx install git+https://github.com/devvf/sgfl.git`.

## Upgrading

run the command `pipx upgrade sgfl` to update to the newest version if available.

## Commands

A full list of commands can be found by running `sgfl --help`.

### To Save

run the command `sgfl save`.

### To Start

`-p` can be added before start to pull files from the relevant repository.

run the command `sgfl start`.
