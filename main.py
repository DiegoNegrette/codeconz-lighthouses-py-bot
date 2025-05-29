import argparse
import random
import time
from concurrent import futures
from collections import deque

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
        self.visited = deque(maxlen=3)  # Keep last 3 visited
        self.target = None
        self.countT = 1

    def manhattan_distance(self, a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y
        my_pos = (cx, cy)

        # Create dictionary for quick lookup
        lighthouses = {(lh.Position.X, lh.Position.Y): lh for lh in turn.Lighthouses}
        lh_here = lighthouses.get(my_pos)

        if lh_here:
            # # Check if connection is possible
            # for conn in turn.Connections:
            #     if (conn.From.X, conn.From.Y) == my_pos and conn.CanConnect:
            #         dest = (conn.To.X, conn.To.Y)
            #         self.visited.append(my_pos)
            #         action = game_pb2.NewAction(
            #             Action=game_pb2.CONNECT,
            #             Destination=game_pb2.Position(X=dest[0], Y=dest[1]),
            #             Energy=min(turn.Energy, 10),
            #         )
            #         self.turn_states.append(BotGameTurn(turn, action))
            #         self.countT += 1
            #         return action

            # If it's not ours, attack
            if lh_here.Owner != self.player_num:
                self.visited.append(my_pos)
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Destination=game_pb2.Position(X=cx, Y=cy),
                    Energy=turn.Energy,
                )
                self.turn_states.append(BotGameTurn(turn, action))
                self.countT += 1
                return action

            # It's ours but no connection — just mark as visited
            self.visited.append(my_pos)

        # Choose a new target if needed
        if not self.target or self.target in self.visited:
            unvisited = [
                pos for pos in lighthouses.keys() if pos not in self.visited
            ]
            if unvisited:
                self.target = min(unvisited, key=lambda pos: self.manhattan_distance(my_pos, pos))
            else:
                self.target = None  # All nearby lighthouses visited

        # Move toward target
        if self.target:
            tx, ty = self.target
            dx = 0 if tx == cx else (1 if tx > cx else -1)
            dy = 0 if ty == cy else (1 if ty > cy else -1)
            action = game_pb2.NewAction(
                Action=game_pb2.MOVE,
                Destination=game_pb2.Position(X=cx + dx, Y=cy + dy),
            )
        else:
            action = game_pb2.NewAction(
                Action=game_pb2.PASS,
                Destination=game_pb2.Position(X=cx, Y=cy),
            )

        self.turn_states.append(BotGameTurn(turn, action))
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
