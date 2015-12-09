from PyQt4.QtCore import QObject, pyqtSignal
from PyQt4.QtNetwork import QTcpServer, QHostAddress
from enum import IntEnum

from base import Client
from connectivity import QTurnClient
from connectivity.turn import TURNState
from decorators import with_logger
from fa.game_connection import GPGNetConnection
from fa.game_process import GameProcess, instance as game_process_instance


class GameSessionState(IntEnum):
    # Game services are entirely off
    OFF = 0
    # We're listening on the game port
    LISTENING = 1
    # We've verified our connectivity and are idle
    IDLE = 2
    # Game has been launched but we're not connected yet
    LAUNCHED = 3
    # Game is running and we're relaying gpgnet commands
    RUNNING = 4


@with_logger
class GameSession(QObject):
    ready = pyqtSignal()

    def __init__(self, client, connectivity):
        QObject.__init__(self)
        self._state = GameSessionState.OFF

        # Subscribe to messages targeted at 'game' from the server
        client.subscribe_to('game', self)

        # Connectivity helper
        self.connectivity = connectivity
        self.connectivity.relay_bound.connect(self.ready.emit)

        # Keep a parent pointer so we can use it to send
        # relay messages about the game state
        self._client = client  # type: Client
        self.me = client.me

        self.game_port = client.gamePort
        self.player = client.me

        # Use the normal lobby by default
        self.init_mode = 0

        # 'GPGNet' TCP listener
        self._game_listener = QTcpServer(self)
        self._game_listener.newConnection.connect(self._new_game_connection)

        # We only allow one game connection at a time
        self._game_connection = None

        self._process = game_process_instance  # type: GameProcess
        self._process.started.connect(self._launched)
        self._process.finished.connect(self._exited)

    @property
    def relay_port(self):
        return self._game_listener.serverPort()

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, val):
        self._state = val

    def listen(self):
        """
        Start listening for remote commands

        Call this in good time before hosting a game,
        e.g. when the host game dialog is being shown.
        """
        assert self.state == GameSessionState.OFF
        self._game_listener.listen(QHostAddress.LocalHost)
        self.state = GameSessionState.LISTENING
        if self.connectivity.state == 'STUN':
            self.connectivity.prepare()
        else:
            self.ready.emit()

    def handle_message(self, message):
        command, args = message.get('command'), message.get('args', [])
        if command == 'SendNatPacket':
            addr_and_port, message = args
            host, port = addr_and_port.split(':')
            self.connectivity.send(message, (host, port))
        elif command == 'CreatePermission':
            addr_and_port = args[0]
            host, port = addr_and_port.split(':')
            self._turn_client.permit((host, port))
        elif command == 'JoinGame':
            addr, login, peer_id = args
        elif command == 'ConnectToPeer':
            addr, login, peer_id = args
        else:
            self._game_connection.send(command, *args)

    def send(self, command_id, args):
        self._logger.info("Outgoing relay message {} {}".format(command_id, args))
        self._client.send({
            'command': command_id,
            'target': 'game',
            'args': args or []
        })

    def _new_game_connection(self):
        self._logger.info("Game connected through GPGNet")
        assert not self._game_connection
        self._game_connection = GPGNetConnection(self._game_listener.nextPendingConnection())
        self._game_connection.messageReceived.connect(self._on_game_message)
        self.state = GameSessionState.RUNNING

    def _on_game_message(self, command, args):
        self._logger.info("Incoming GPGNet: {} {}".format(command, args))
        if command == "GameState":
            if args[0] == 'Idle':
                # autolobby, port, nickname, uid, hasSupcom
                self._game_connection.send("CreateLobby",
                                           self.init_mode,
                                           self.game_port + 1,
                                           self.me.login,
                                           self.me.id,
                                           1)
            elif args[0] == 'Lobby':
                # TODO: Eagerly initialize the game by hosting/joining early
                pass
        self.send(command, args)

    def _turn_state_changed(self, val):
        if val == TURNState.BOUND:
            self.ready.emit()

    def _launched(self):
        self._logger.info("Game has started")

    def _exited(self, status):
        self._logger.info("Game has exited with status code: {}".format(status))
        self.send('GameState', ['Ended'])