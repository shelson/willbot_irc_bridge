**Willbot IRC Bridge Plugin**

This is a plugin for the awesome Will Bot (https://github.com/skoczen/will/ and http://skoczen.github.io/will).

This plugin will connect to your IRC server of choice, and dutifully relay messages in both directions between same-named Hipchat rooms and IRC channels. This can be useful for letting less technical people participate in IRC discussions, or even to bridge the gap in a move between IRC and Hipchat.

Very alpha currently, but is in working order.

Usage:

 1. Get a Will instance running using the instructions at http://skoczen.github.io/will
 2. Clone this repo into a directory in your plugins directory
 3. pip install -r requirements.txt to install the python dependencies
 4. Add the following to your config.py, modifying as required:
```
> IRC_BRIDGE_IRC_SERVER="irc.server.name" 
> IRC_BRIDGE_IRC_PORT="6667"
> IRC_BRIDGE_NICKNAME="will_bot" 
> IRC_BRIDGE_USE_SSL=False
> IRC_BRIDGE_CHANNELS = [ "#list", "#of", "#irc", "#channels" ]
> IRC_BRIDGE_NICKNAME="will_bot"
```
 5. Make sure your Will Bot is configured to join the Hipchat rooms with the same name as the IRC channels (without the #). Note that this is currently case-sensitive when relaying messages from IRC to Hipchat, which can cause issues with Hipchat rooms with capitals in the name.
 6. In a Hipchat room where Will Bot has joined, issue the command "@willbotname connect to IRC". This will instruct the plugin to connect to the IRC server and join the list of channels, and start relaying messages. This should be all that you need to do to make it work.

Due to rate-limiting of the Hipchat messaging API, messages are relayed to Hipchat in batches every 2 seconds, this means that theoretically the plugin should never go over the 30/minute per-room message limit.
