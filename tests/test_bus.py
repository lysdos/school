import asyncio
import threading

from app.bus import EventBus


def test_publish_reaches_subscriber():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        bus._publish({"a": 1})
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert asyncio.run(main()) == {"a": 1}


def test_unsubscribe_stops_delivery():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus._publish({"a": 1})
        assert q.empty()
        return bus.subscriber_count

    assert asyncio.run(main()) == 0


def test_publish_without_bound_loop_is_noop():
    EventBus().publish_threadsafe({"a": 1})  # 不应抛异常


def test_slow_subscriber_is_dropped_not_blocking():
    async def main():
        bus = EventBus(maxsize=2)
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        for i in range(10):
            bus._publish(i)
        return q.qsize()

    assert asyncio.run(main()) == 2


def test_publish_threadsafe_from_worker_thread():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        q = bus.subscribe()
        worker = threading.Thread(target=lambda: bus.publish_threadsafe("hello"))
        worker.start()
        worker.join()
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert asyncio.run(main()) == "hello"


def test_multiple_subscribers_all_receive():
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        a, b = bus.subscribe(), bus.subscribe()
        bus._publish("x")
        return (await asyncio.wait_for(a.get(), 1.0),
                await asyncio.wait_for(b.get(), 1.0))

    assert asyncio.run(main()) == ("x", "x")
