from will.plugin import WillPlugin
from will.mixins import HipChatMixin
from will.decorators import respond_to, periodic, hear, randomly, route, rendered_template, require_settings
from will import settings

# sorting out html
import cgi

from multiprocessing import Process, Queue

# Twisted
from twisted.internet import ssl, reactor, protocol
from twisted.internet.protocol import ClientFactory, Protocol
from twisted.words.protocols import irc

# Queues for IRC<->HC
irc_to_hipchat_queue = Queue()
hipchat_to_irc_queue = Queue()
connected_to_irc = False
irc_bridge_verbose = False

class IrcBridgePlugin(WillPlugin):
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
                      "IRC_BRIDGE_CHANNELS",
                      "IRC_BRIDGE_NICKNAME")
    @respond_to("^connect to irc$")
    def connect(self, message):
        global connected_to_irc
        if not connected_to_irc:
            self.irc_host = settings.IRC_BRIDGE_IRC_SERVER
            self.irc_port = int(settings.IRC_BRIDGE_IRC_PORT)
            self.channels = settings.IRC_BRIDGE_CHANNELS
            self.irc_nickname = settings.IRC_BRIDGE_NICKNAME
            if hasattr(settings, "IRC_BRIDGE_PASSWORD"):
                self.irc_password = settings.IRC_BRIDGE_PASSWORD
            else:
                self.irc_password = ""
            if hasattr(settings, "IRC_BRIDGE_USE_SSL"):
                self.use_ssl = settings.IRC_BRIDGE_USE_SSL
            else:
                self.use_ssl = False

            p = Process(target=self.bootstrap_irc)
            p.start()
            connected_to_irc = True
            self.reply(message, "Connecting to IRC")
        else:
            self.reply(message, "Already connected")


    # This is where we grab hipchat messages and put them in a queue to head to IRC
    @require_settings("IRC_BRIDGE_IRC_SERVER",
                      "IRC_BRIDGE_IRC_PORT",
                      "IRC_BRIDGE_CHANNELS")
    @hear("^.*$")
    def send_to_irc(self, message):
        global hipchat_to_irc_queue
        global connected_to_irc

        if connected_to_irc:
            hipchat_to_irc_queue.put({"channel": "#%s" % message.room.name.encode('utf-8'), "user": message.sender.mention_name.encode('utf-8'), "message": message["body"].encode('utf-8')})

            if irc_bridge_verbose:
                self.reply(message, "Sent to IRC queue, queue length is now %d" % hipchat_to_irc_queue.qsize())

class IrcBot(irc.IRCClient):
    """A IRC bot."""

    # this should be overwritten
    nickname = "willbot"

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)

    # callbacks for events

    def signedOn(self):
        """Called when bot has succesfully signed on to server."""
        for channel in self.factory.channels:
            self.join(channel)

    def joined(self, channel):
        """This will get called when the bot joins the channel."""
        pass

    def privmsg(self, user, channel, msg):
        """This will get called when the bot receives a message."""
        user = user.split('!', 1)[0]

        self.irc_to_hipchat_queue.put({"channel": channel.split("#")[1], "user": user, "message": msg})

        # Check to see if they're sending me a private message
        if channel == self.nickname:
            msg = "I don't respond well to PM's yet."
            self.msg(user, msg)
            return

    def action(self, user, channel, msg):
        """This will get called when the bot sees someone do an action."""
        user = user.split('!', 1)[0]
        self.irc_to_hipchat_queue.put({"channel": channel.split("#")[1], "user": user, "message": msg})

    # irc callbacks

    def irc_NICK(self, prefix, params):
        """Called when an IRC user changes their nickname."""
        old_nick = prefix.split('!')[0]
        new_nick = params[0]


    # For fun, override the method that determines how a nickname is changed on
    # collisions. The default method appends an underscore.
    def alterCollidedNick(self, nickname):
        """
        Generate an altered version of a nickname that caused a collision in an
        effort to create an unused related name for subsequent registration.
        """
        return nickname + '^'



class IrcHipchatBridge(protocol.ClientFactory, HipChatMixin):
    def __init__(self, host, port, password, nickname, channels, use_ssl, hipchat_to_irc_queue, irc_to_hipchat_queue):
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
        print "connection failed:", reason
        reactor.stop()


    def update_irc(self):
        if hasattr(self.ircbot, "msg"):
            while not self.hipchat_to_irc_queue.empty():
                m = self.hipchat_to_irc_queue.get()
                # light touch html sanitisation for Confluence messages
                # make this more generic in future as it's a hack
                if m["user"] == "Confluence":
                    soup = BeautifulSoup.BeautifulSoup(m["message"])
                    message = " ".join(soup.getText(" ").split(" ")[2:])
                else:
                    message = m["message"]
                self.ircbot.msg(m["channel"], "<%s> %s" % (m["user"], message))
        else:
            print "Not connected yet"

        reactor.callLater(self.update_interval, self.update_irc)

    def update_hipchat(self):
        # this has some smarts because of the hipchat ratelimiting
        # so every time it runs it batches up the messages for a 
        # room and sends them as a single hipchat "message"
        # or that's the theory at least
        todo = {}
        while not self.irc_to_hipchat_queue.empty():
            m = self.irc_to_hipchat_queue.get()
            try:    
                todo[m["channel"]].append((m["user"], m["message"]))
            except KeyError:
                todo[m["channel"]] = [(m["user"], m["message"])]

        for channel in todo:
            # create some nice html to send to hipchat
            html_message = ""
            for (user, msg) in todo[channel]:
                html_message = html_message + "<b>%s</b> %s<br>" % (cgi.escape(user), cgi.escape(msg))
            self.send_room_message(channel, html_message, html=True)

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


