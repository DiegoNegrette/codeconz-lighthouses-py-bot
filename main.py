import argparse
import random
import time
from concurrent import futures

import grpc
from google.protobuf import json_format
from grpc import RpcError

from internal.handler.coms import game_pb2
from internal.handler.coms import game_pb2_grpc as game_grpc

timeout_to_response = 1  # 1 second


class BotGameTurn:
    def __init__(self, turn, action):
        self.turn = turn
        self.action = action


class BotGame:
    def __init__(self, player_num=None):
        self.player_num = player_num
        self.initial_state = None
        self.turn_states = []
        self.countT = 1
        self.last_cell = True

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y

        lighthouses = dict()
        for lh in turn.Lighthouses:
            lighthouses[(lh.Position.X, lh.Position.Y)] = lh

        # Si estamos en un faro...
        if (cx, cy) in lighthouses:
            # Conectar con faro remoto válido si podemos
            if lighthouses[(cx, cy)].Owner == self.player_num:
                possible_connections = []
                for dest in lighthouses:
                    # No conectar con sigo mismo
                    # No conectar si no tenemos la clave
                    # No conectar si ya existe la conexión
                    # No conectar si no controlamos el destino
                    # Nota: no comprobamos si la conexión se cruza.
                    if (
                        dest != (cx, cy)
                        and lighthouses[dest].HaveKey
                        and [cx, cy] not in lighthouses[dest].Connections
                        and lighthouses[dest].Owner == self.player_num
                    ):
                        possible_connections.append(dest)

                if possible_connections:
                    possible_connection = random.choice(possible_connections)
                    action = game_pb2.NewAction(
                        Action=game_pb2.CONNECT,
                        Destination=game_pb2.Position(
                            X=possible_connection[0], Y=possible_connection[1]
                        ),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)

                    self.countT += 1
                    return action

            # 60% de posibilidades de atacar el faro
            if random.randrange(100) < 60:
                energy = random.randrange(turn.Energy + 1)
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=energy,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                return action

        # Mover aleatoriamente
        moves = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
        # move = random.choice(moves)
        # action = game_pb2.NewAction(
        #     Action=game_pb2.MOVE,
        #     Destination=game_pb2.Position(
        #         X=turn.Position.X + move[0], Y=turn.Position.Y + move[1]
        #     ),
        # )
        if self.last_cell:
            action = game_pb2.NewAction(
                Action=game_pb2.MOVE,
                Destination=game_pb2.Position(
                    X=0, Y=14
                ),
            )
        else:
            action = game_pb2.NewAction(
                Action=game_pb2.MOVE,
                Destination=game_pb2.Position(
                    X=14, Y=14
                ),
            )
        self.last_cell = not self.last_cell

        bgt = BotGameTurn(turn, action)
        self.turn_states.append(bgt)

        self.countT += 1
        return action


class BotComs:
    def __init__(self, bot_name, my_address, game_server_address, verbose=False):
        self.bot_id = None
        self.bot_name = bot_name
        self.my_address = my_address
        self.game_server_address = game_server_address
        self.verbose = verbose

    def wait_to_join_game(self):
        channel = grpc.insecure_channel(self.game_server_address)
        client = game_grpc.GameServiceStub(channel)

        player = game_pb2.NewPlayer(name=self.bot_name, serverAddress=self.my_address)

        while True:
            try:
                player_id = client.Join(player, timeout=timeout_to_response)
                self.bot_id = player_id.PlayerID
                print(f"Joined game with ID {player_id.PlayerID}")
                if self.verbose:
                    print(json_format.MessageToJson(player_id))
                break
            except RpcError as e:
                print(f"Could not join game: {e.details()}")
                time.sleep(1)

    def start_listening(self):
        print("Starting to listen on", self.my_address)

        # configure gRPC server
        grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=(ServerInterceptor(),),
        )

        # registry of the service
        cs = ClientServer(bot_id=self.bot_id, verbose=self.verbose)
        game_grpc.add_GameServiceServicer_to_server(cs, grpc_server)

        # server start
        grpc_server.add_insecure_port(self.my_address)
        grpc_server.start()

        try:
            grpc_server.wait_for_termination()  # wait until server finish
        except KeyboardInterrupt:
            grpc_server.stop(0)


class ServerInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        start_time = time.time_ns()
        method_name = handler_call_details.method

        # Invoke the actual RPC
        response = continuation(handler_call_details)

        # Log after the call
        duration = time.time_ns() - start_time
        print(f"Unary call: {method_name}, Duration: {duration:.2f} nanoseconds")
        return response


class ClientServer(game_grpc.GameServiceServicer):
    def __init__(self, bot_id, verbose=False):
        self.bg = BotGame(bot_id)
        self.verbose = verbose

    def Join(self, request, context):
        return None

    def InitialState(self, request, context):
        print("Receiving InitialState")
        if self.verbose:
            print(json_format.MessageToJson(request))
        self.bg.initial_state = request
        return game_pb2.PlayerReady(Ready=True)

    def Turn(self, request, context):
        print(f"Processing turn: {self.bg.countT}")
        if self.verbose:
            print(json_format.MessageToJson(request))
        action = self.bg.new_turn_action(request)
        return action


def ensure_params():
    parser = argparse.ArgumentParser(description="Bot configuration")
    parser.add_argument("--bn", type=str, default="random-bot", help="Bot name")
    parser.add_argument("--la", type=str, required=True, help="Listen address")
    parser.add_argument("--gs", type=str, required=True, help="Game server address")

    args = parser.parse_args()

    if not args.bn:
        raise ValueError("Bot name is required")
    if not args.la:
        raise ValueError("Listen address is required")
    if not args.gs:
        raise ValueError("Game server address is required")

    return args.bn, args.la, args.gs


def main():
    verbose = False
    bot_name, listen_address, game_server_address = ensure_params()

    bot = BotComs(
        bot_name=bot_name,
        my_address=listen_address,
        game_server_address=game_server_address,
        verbose=verbose,
    )
    bot.wait_to_join_game()
    bot.start_listening()


if __name__ == "__main__":
    main()
