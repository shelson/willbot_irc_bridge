from will.plugin import WillPlugin
from will.mixins import HipChatMixin
from will.decorators import respond_to, periodic, hear, randomly, route, rendered_template, require_settings
from will import settings

# imports so we can make our own send_room_message method
import traceback
import requests
import json
import time

# sorting out html
import BeautifulSoup
import logging
import re
import urllib

from multiprocessing import Process, Queue

# Twisted
from twisted.internet import ssl, reactor, protocol
from twisted.internet.protocol import ClientFactory, Protocol
from twisted.words.protocols import irc

# because we're going off-piste and using our own api send methond
# so we can monitor the rate-limiting
ROOM_NOTIFICATION_URL = "https://%(server)s/v2/room/%(room_id)s/notification?auth_token=%(token)s"


# Queues for IRC<->HC
irc_to_hipchat_queue = Queue()
hipchat_to_irc_queue = Queue()
irc_bridge_verbose = False


class IrcBridgePlugin(WillPlugin):
    def __init__(self):
        self.connected_to_irc = False
        self.connect()

    def bootstrap_irc(self):
        self.ircbot = IrcHipchatBridge(self.irc_host,
                                       self.irc_port,
                                       self.irc_password,
                                       self.irc_nickname,
                                       self.channels,
                                       self.use_ssl,
                                       hipchat_to_irc_queue,
                                       irc_to_hipchat_queue)
        self.ircbot.run()

    @require_settings("IRC_BRIDGE_IRC_SERVER",
                      "IRC_BRIDGE_IRC_PORT",
                      "IRC_BRIDGE_NICKNAME")

    def connect(self):
        if not self.connected_to_irc:
            self.irc_host = settings.IRC_BRIDGE_IRC_SERVER
            self.irc_port = int(settings.IRC_BRIDGE_IRC_PORT)
            self.channels = [ "#%s" % i for i in settings.ROOMS ]
            self.irc_nickname = settings.IRC_BRIDGE_NICKNAME
            if hasattr(settings, "IRC_BRIDGE_PASSWORD"):
                self.irc_password = settings.IRC_BRIDGE_PASSWORD
            else:
                self.irc_password = ""
            if hasattr(settings, "IRC_BRIDGE_USE_SSL"):
                self.use_ssl = settings.IRC_BRIDGE_USE_SSL
            else:
                self.use_ssl = False
            if hasattr(settings, "IRC_BRIDGE_USERS_STRIP_HTML"):
                self.irc_bridge_users_strip_html = settings.IRC_BRIDGE_USERS_STRIP_HTML
            else:
                self.irc_bridge_users_strip_html = []
            if hasattr(settings, "IRC_BRIDGE_USERS_STRIP_NAME"):
                self.irc_bridge_users_strip_name = settings.IRC_BRIDGE_USERS_STRIP_NAME
            else:
                self.irc_bridge_users_strip_name = []

            p = Process(target=self.bootstrap_irc)
            p.start()
            self.connected_to_irc = True
            self.say("Connecting to IRC")


    # This is where we grab hipchat messages and put them in a queue to head to IRC
    @require_settings("IRC_BRIDGE_IRC_SERVER",
                      "IRC_BRIDGE_IRC_PORT")
    @hear("^(.|\n)*$", multiline=True)
    def send_to_irc(self, message):
        global hipchat_to_irc_queue

        if self.connected_to_irc:
            try:
                sender = message.sender.mention_name
            except AttributeError:
                # sometimes the sender is really munged
                try:
                    sender = message["mucnick"].replace(" ", "_")
                except AttributeError:
                    logging.error("Couldn't work out who sent message, giving up")
                    return

            if sender in self.irc_bridge_users_strip_html:
                soup = BeautifulSoup.BeautifulSoup(message['body'])
                hipchat_message =soup.getText(" ")
            else:
                hipchat_message = message['body']

            if sender in self.irc_bridge_users_strip_name:
                sender = ""

            for msgline in hipchat_message.split(u'\n'):
                logging.debug("Added a message to hipchat_to_irc_queue; queue length is now %d", hipchat_to_irc_queue.qsize())
                hipchat_to_irc_queue.put({"channel": "#%s" % message.room.name.encode('utf-8'), "user": sender.encode('utf-8'), "message": msgline.encode('utf-8')})

            if irc_bridge_verbose:
                self.reply(message, "Sent to IRC queue, queue length is now %d" % hipchat_to_irc_queue.qsize())

class IrcBot(irc.IRCClient):
    """A IRC bot."""

    # this should be overwritten
    nickname = "willbot"
    relay = True

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)

    # callbacks for events

    def signedOn(self):
        """Called when bot has succesfully signed on to server."""
        for channel in self.factory.channels:
            self.join(channel)
            self.factory.stats.channels[channel.lower()] = IrcHipchatChannelStat() 

    def joined(self, channel):
        """This will get called when the bot joins the channel."""
        pass

    def privmsg(self, user, channel, msg):
        """This will get called when the bot receives a message."""

        user = user.split('!', 1)[0]

        # Check to see if they're sending me a private message
        if channel == self.nickname:
            if irc.stripFormatting(msg) == "stats":
                self.stats(user)
            elif irc.stripFormatting(msg) == "stats detail":
                self.stats(user, detail=True)
            else:
                msg = "I don't respond well to PM's yet."
                self.msg(user, msg)
            return

        self.irc_to_hipchat_queue.put({"channel": channel.split("#")[1], "user": user, "message": irc.stripFormatting(msg)})
        self.factory.stats.irc_relay_enqueued += 1
        self.factory.stats.channels[channel.lower()].irc_relay_enqueued +=1

    def action(self, user, channel, msg):
        """This will get called when the bot sees someone do an action."""
        user = user.split('!', 1)[0]
        self.irc_to_hipchat_queue.put({"channel": channel.split("#")[1], "user": user, "message": irc.stripFormatting(msg)})
        self.factory.stats.irc_relay_enqueued += 1
        self.factory.stats.channels[channel.lower()].irc_relay_enqueued += 1

    def stats(self, user, detail=False):
        """Output some statistics about our lovely self """
        msg = "irc_to_hipchat_queue length: %d" % self.factory.irc_to_hipchat_queue.qsize()
        self.msg(user, msg)
        msg = "hipchat_to_irc_queue length: %d" % self.factory.hipchat_to_irc_queue.qsize()
        self.msg(user, msg)
        msg = "I have enqueued %d messages from IRC and sent %d of these to Hipchat via %d API calls" % (self.factory.stats.irc_relay_enqueued, self.factory.stats.irc_relay_dequeued, self.factory.stats.hipchat_api_count)
        self.msg(user, msg)
        msg = "I have dequeued %d messages from HipChat and sent %d of these" % (self.factory.stats.hipchat_relay_dequeued, self.factory.stats.hipchat_relay_count)
        self.msg(user, msg)
        if (int(self.factory.stats.ratelimit_reset) - int(time.time())) < 0:
            msg = "x-ratelimit-remaining is currently %s out of %s" % (self.factory.stats.ratelimit_limit, self.factory.stats.ratelimit_limit)
        else:
            msg = "x-ratelimit-remaining is currently %s out of %s with %d seconds to go" % (self.factory.stats.ratelimit_remaining, self.factory.stats.ratelimit_limit, (int(self.factory.stats.ratelimit_reset) - int(time.time())))
        self.msg(user, msg)

        if detail:
            for channel in self.factory.stats.channels:
                msg = "%s: %s enqueued, %s dequeued, %s API calls, %s hipchat-to-irc dequeued, %s hipchat-to-irc" % (channel, self.factory.stats.channels[channel].irc_relay_enqueued, self.factory.stats.channels[channel].irc_relay_dequeued, self.factory.stats.channels[channel].hipchat_api_count, self.factory.stats.channels[channel].hipchat_relay_dequeued, self.factory.stats.channels[channel].hipchat_relay_count)
                self.msg(user, msg)


    # irc callbacks

    def irc_NICK(self, prefix, params):
        """Called when an IRC user changes their nickname."""
        old_nick = prefix.split('!')[0]
        new_nick = params[0]

    def irc_TOPIC(self, prefix, params):
        user = prefix.split('!', 1)[0]
        self.irc_to_hipchat_queue.put({"channel": params[0].split("#")[1], "user": user, "topic": params[1]})

    def irc_unknown(self, prefix, command, params):
        """ Handle unknown IRC command """
        if command == "INVITE":
            # TODO: Join the room and start relaying
            #self.join(params[1])
            # For now, just reply back to the user and respond
            user = prefix.split('!')[0]
            self.msg(user, "Sorry, please follow y/rb-hipchat-irc-bridge if you want to bridge a channel!")

    # For fun, override the method that determines how a nickname is changed on
    # collisions. The default method appends an underscore.
    def alterCollidedNick(self, nickname):
        """
        Generate an altered version of a nickname that caused a collision in an
        effort to create an unused related name for subsequent registration.
        """
        self.relay = False
        logging.error("Found another IRC user with nick %s, NOT RELAYING.", nickname)
        return nickname + '^'

class IrcHipchatChannelStat(object):
    def __init__(self):
        self.irc_relay_enqueued = 0
        self.irc_relay_dequeued = 0
        self.hipchat_api_count = 0
        self.hipchat_relay_dequeued = 0
        self.hipchat_relay_count = 0

class IrcHipchatStats(object):
    def __init__(self):
        self.channels = {}
        self.irc_relay_enqueued = 0
        self.irc_relay_dequeued = 0
        self.hipchat_api_count = 0
        self.hipchat_relay_dequeued = 0
        self.hipchat_relay_count = 0
        self.ratelimit_remaining = 0
        self.ratelimit_limit = 0
        self.ratelimit_reset = 0

class IrcHipchatBridge(protocol.ClientFactory, HipChatMixin):
    def __init__(self, host, port, password, nickname, channels, use_ssl, hipchat_to_irc_queue, irc_to_hipchat_queue, relay=True):
        self.ircbot = None
        self.channels = channels
        self.irc_host = host
        self.irc_port = port
        self.irc_password = password
        self.nickname = nickname
        self.use_ssl = use_ssl
        self.hipchat_to_irc_queue = hipchat_to_irc_queue
        self.irc_to_hipchat_queue = irc_to_hipchat_queue
        # hipchat ratelimits to 30 requests/min so we run
        # the update thread every 2 seconds
        self.update_interval = 2
        self.relay = relay
        self.stats = IrcHipchatStats()

    def buildProtocol(self, addr):
        self.ircbot = IrcBot()
        self.ircbot.nickname = self.nickname
        self.ircbot.password = self.irc_password
        self.ircbot.factory = self
        self.ircbot.irc_to_hipchat_queue = self.irc_to_hipchat_queue
        return self.ircbot

    def clientConnectionLost(self, connector, reason):
        """If we get disconnected, reconnect to server."""
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        logging.error("connection failed:", reason)
        reactor.stop()


    def update_irc(self):
        if hasattr(self.ircbot, "msg"):
            # update relay state
            self.relay = self.ircbot.relay
            while not self.hipchat_to_irc_queue.empty():
                m = self.hipchat_to_irc_queue.get()
                message = m["message"]
                try:
                    self.stats.hipchat_relay_dequeued += 1
                    self.stats.channels[m['channel'].lower()].hipchat_relay_dequeued += 1
                except KeyError, e:
                    logging.info("Exception caught trying to update hipchat to irc stats on enqueue")
                    logging.info(e)

                if not re.match("^\s*$", message):
                    logging.debug("HC->IRC queue not empty, attempting to relay")
                    if self.relay:
                        try:
                            if m["user"] == "":
                                self.ircbot.msg(m["channel"], "%s" % message.encode('utf-8'))
                            else:
                                self.ircbot.msg(m["channel"], "<%s> %s" % (m["user"], message.encode('utf-8')))
                            try:
                                self.stats.hipchat_relay_count += 1
                                self.stats.channels[m['channel'].lower()].hipchat_relay_count += 1
                            except KeyError, e:
                                logging.info("Exception caught trying to update Hipchat to IRC stats")
                                logging.info(e)
                        except Exception, e:
                            logging.error("Exception caught trying to relay message to IRC")
                            logging.exception(e)
                    else:
                        logging.debug("Not relaying messages %s", message)
        else:
            logging.error("Can't relay message to IRC; Not connected yet")

        reactor.callLater(self.update_interval, self.update_irc)

    def local_send_room_message(self, room_id, message_body, html=False, color="green", notify=False, **kwargs):
        if kwargs:
            logging.warn("Unknown keyword args for send_room_message: %s" % kwargs)

        format = "text"
        if html:
            format = "html"

        try:
            # https://www.hipchat.com/docs/apiv2/method/send_room_notification
            url = ROOM_NOTIFICATION_URL % {"server": settings.HIPCHAT_SERVER,
                                           "room_id": room_id,
                                           "token": settings.V2_TOKEN}
            data = {
                "message": message_body,
                "message_format": format,
                "color": color,
                "notify": notify,
            }
            headers = {'Content-type': 'application/json', 'Accept': 'text/plain'}
            return requests.post(url, headers=headers, data=json.dumps(data), **settings.REQUESTS_OPTIONS)
        except:
            logging.critical("Error in send_room_message: \n%s" % traceback.format_exc())

    def update_hipchat(self):
        # this has some smarts because of the hipchat ratelimiting
        # so every time it runs it batches up the messages for a
        # room and sends them as a single hipchat "message"
        # or that's the theory at least
        todo = {}
        if not (self.stats.ratelimit_remaining == 0 and self.stats.ratelimit_reset > time.time()):
            while not self.irc_to_hipchat_queue.empty():
                m = self.irc_to_hipchat_queue.get()
                if "topic" in m:
                    # Truncate to 249 characters as Hipchat's max is 250
                    topic = m["topic"][:249]
                    self.set_room_topic(m["channel"], topic)
                    logging.info("Setting topic for %s to %s" % (m['channel'], topic))
                else:
                    try:
                        todo[m["channel"]].append((m["user"], m["message"]))
                    except KeyError:
                        todo[m["channel"]] = [(m["user"], m["message"])]

                    try: 
                        self.stats.irc_relay_dequeued += 1
                        self.stats.channels[('#' + m['channel']).lower()].irc_relay_dequeued += 1
                    except KeyError:
                        logging.error("Failed to update channel stats for %s" % ('#' + m['channel']).lower())
                        logging.info(e)

            for channel in todo:
                txt_message = ""
                for (user, msg) in todo[channel]:
                    txt_message = txt_message + "<%s> %s\n" % (user, urllib.unquote(msg))

                if self.relay:
                    try:
                        response = self.local_send_room_message(channel, txt_message, html=False, notify=True)
                        try:
                            self.stats.ratelimit_remaining = response.headers['x-ratelimit-remaining']
                            self.stats.ratelimit_limit = response.headers['x-ratelimit-limit']
                            self.stats.ratelimit_reset = response.headers['x-ratelimit-reset']
                            self.stats.hipchat_api_count += 1
                            self.stats.channels[('#' + channel).lower()].hipchat_api_count += 1
                        except KeyError, e:
                            logging.error("Failed to update channel stats for %s" % ('#' + m['channel']).lower())
                            logging.info(e)

                    except Exception, e:
                        logging.error("Failed to relay message to hipchat!")
                        logging.info(e)
                else:
                    logging.debug("Not relaying message %s",txt_message)
        else:
            logging.debug("Hit Hipchat ratelimit; delaying attempt to relay for %d seconds", self.update_interval)

        # schedule ourselves for another run
        reactor.callLater(self.update_interval, self.update_hipchat)

    def run(self):
        # set up our update loop with a small delay for connecting
        # it will then call itself using self.update_interval as
        # the interval
        reactor.callLater(1, self.update_irc)
        reactor.callLater(1, self.update_hipchat)
        if self.use_ssl:
            reactor.connectSSL(self.irc_host, self.irc_port, self, ssl.ClientContextFactory())
        else:
            reactor.connectTCP(self.irc_host, self.irc_port, self)

        reactor.run()


