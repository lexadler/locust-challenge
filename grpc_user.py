import time
from collections.abc import Callable
from functools import cached_property
from typing import Any

import gevent
import grpc
import grpc.experimental.gevent as grpc_gevent
from grpc_interceptor import ClientInterceptor
from locust import User
from locust.env import Environment
from locust.exception import LocustError

# patch grpc so that it uses gevent instead of asyncio
grpc_gevent.init_gevent()


class LocustInterceptor(ClientInterceptor):
    """gRPC request interceptor sends events to Locust."""

    def __init__(self, environment: Environment, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.env = environment

    def intercept(
        self,
        method: Callable,
        request_or_iterator: Any,
        call_details: grpc.ClientCallDetails,
    ) -> Any:
        response_or_iterator = None
        exception = None
        start_perf_counter = time.perf_counter()
        response_length = 0
        try:
            # If it was unary, handle any exception raised
            response_or_iterator = method(request_or_iterator, call_details)
        except grpc.RpcError as e:
            exception = e
        if hasattr(response_or_iterator, '__iter__'):
            # Intercept streaming
            try:
                for resp in response_or_iterator:
                    response_length += resp.ByteSize()
            except grpc.RpcError as e:
                exception = e
        else:
            response_length = response_or_iterator.result().ByteSize()

        # Fire event to Locust with the response times of the gRPC request in locust
        # as well as any errors that would be returned by the gRPC server.
        self.env.events.request.fire(
            request_type='grpc',
            name=call_details.method,
            response_time=(time.perf_counter() - start_perf_counter) * 1000,
            response_length=response_length,
            response=response_or_iterator,
            context=None,
            exception=exception,
        )

        return response_or_iterator


class GrpcUser(User):
    """Generic GrpcUser base class sends events to Locust using an interceptor."""

    abstract = True
    stub_class = None

    def __init__(self, environment: Environment):
        super().__init__(environment)
        for attr_value, attr_name in ((self.host, 'host'), (self.stub_class, 'stub_class')):
            if attr_value is None:
                raise LocustError(f'You must specify the {attr_name}.')

        self._channel = grpc.insecure_channel(self.host)
        interceptor = LocustInterceptor(environment=environment)
        self._channel = grpc.intercept_channel(self._channel, interceptor)
        self.stub = self.stub_class(self._channel)

        self._background_tasks: set[gevent.Greenlet] = set()  # A set (hash map) to store background tasks (greenlets)
        self._access_token: str | None = None

    def _add_background_task(self, func: Callable, delay: int = 0):
        """
        Schedule a background task to run after a specified delay using gevent.

        Args:
            func (Callable): The function to run in the background.
            delay (int, optional): Delay in seconds before executing the task. Defaults to 0.

        Notes:
            The greenlet is tracked in the `self._background_tasks` set until it completes or terminates,
            at which point it is automatically removed via the `self._greenlet_done_callback` callback.
        """
        greenlet = gevent.spawn_later(delay, func)
        greenlet.link(self._greenlet_done_callback)
        if not greenlet.dead:
            self._background_tasks.add(greenlet)

    def _greenlet_done_callback(self, greenlet: gevent.Greenlet):
        """
        Callback function triggered when a background greenlet completes or terminates due to an error.

        Args:
            greenlet: The greenlet instance that has finished or failed.

        Effect:
            Removes the greenlet from the `self._background_tasks` set to ensure proper cleanup.
        """
        self._background_tasks.discard(greenlet)

    def on_stop(self):
        """
        Called when the Locust user is stopping.

        Effect:
            Terminates all active background greenlets that were spawned during the user's lifecycle.
            Uses `gevent.killall()` to ensure no background tasks continue running after the user stops.
        """
        if self._background_tasks:
            gevent.killall(self._background_tasks, block=True, timeout=30)

    @cached_property
    def _auth_metadata(self) -> list[tuple[str, str]]:
        """
        An attribute `self._access_token` must be set in subclasses to enable the use of the `_auth_metadata` property,
        which returns authentication metadata for gRPC requests in the form required by the `metadata` argument.
        """
        if self._access_token is None:
            raise NotImplementedError('An attribute `self._access_token` was not set!')
        return [('authorization', f'Bearer {self._access_token}')]
