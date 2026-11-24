import time
import threading
import pytest
from unittest.mock import patch
from backend.llm_queue import LLMQueueManager, LLMPriority, LLMTask

def test_queue_priority_ordering():
    """Verify that Priority 1 (INTERACTIVE) tasks jump ahead of Priority 3 (BACKGROUND) tasks."""
    mgr = LLMQueueManager(pacing_seconds=0.01)
    execution_order = []
    task1_started = threading.Event()
    task1_can_finish = threading.Event()

    def blocking_task():
        task1_started.set()
        task1_can_finish.wait(timeout=3.0)
        execution_order.append("blocking")
        return "blocking"

    def record_task(val):
        execution_order.append(val)
        return val

    # 1. Start blocking task so worker is busy
    t_block = LLMTask(blocking_task, (), {}, priority=LLMPriority.BACKGROUND, task_name="block")
    mgr._queue.put(t_block)
    assert task1_started.wait(timeout=2.0) is True

    # 2. While worker is busy, queue a Background task (P3), then an Interactive task (P1)
    t_bg = LLMTask(record_task, ("bg_task",), {}, priority=LLMPriority.BACKGROUND, task_name="bg")
    t_inter = LLMTask(record_task, ("interactive_task",), {}, priority=LLMPriority.INTERACTIVE, task_name="interactive")
    
    mgr._queue.put(t_bg)
    mgr._queue.put(t_inter)

    # 3. Release the blocking task
    task1_can_finish.set()

    # 4. Wait for all to complete
    t_block.future.result(timeout=2.0)
    t_inter.future.result(timeout=2.0)
    t_bg.future.result(timeout=2.0)

    # Interactive MUST have run before Background
    assert execution_order == ["blocking", "interactive_task", "bg_task"]

def test_queue_exception_propagation():
    """Verify that exceptions raised during task execution are propagated to the caller's future."""
    mgr = LLMQueueManager(pacing_seconds=0.01)

    def failing_task():
        raise ValueError("Inference failed dramatically")

    with pytest.raises(ValueError, match="Inference failed dramatically"):
        mgr.submit(failing_task, priority=LLMPriority.ON_DEMAND, timeout=2.0)

def test_queue_status_metrics():
    """Verify that get_status returns accurate queue depth, metrics, and backpressure flags."""
    mgr = LLMQueueManager(pacing_seconds=0.01)
    status = mgr.get_status()

    assert "queue_depth" in status
    assert "total_processed" in status
    assert "is_backpressure_high" in status
    assert isinstance(status["is_backpressure_high"], bool)

def test_queue_clear_background_tasks():
    """Verify that clear_background_tasks cancels only pending P3 tasks."""
    mgr = LLMQueueManager(pacing_seconds=0.01)

    def dummy():
        return 1

    t_bg = LLMTask(dummy, (), {}, priority=LLMPriority.BACKGROUND)
    t_on_demand = LLMTask(dummy, (), {}, priority=LLMPriority.ON_DEMAND)

    mgr._queue.put(t_bg)
    mgr._queue.put(t_on_demand)

    cleared = mgr.clear_background_tasks()
    assert cleared == 1
    assert t_bg.future.cancelled() is True
    assert t_on_demand.future.cancelled() is False

def test_generate_text_local_first_policy():
    """Verify generate_text does not call cloud APIs when local models fail and ALLOW_CLOUD_FALLBACK is False."""
    from backend.ai_helper import _execute_generate_text
    
    with patch("backend.ai_helper.call_lmstudio", side_effect=Exception("LM Studio down")), \
         patch("backend.ai_helper.call_unsloth", side_effect=Exception("Unsloth down")), \
         patch("backend.ai_helper.call_ollama", side_effect=Exception("Ollama down")), \
         patch("backend.ai_helper.call_openai") as mock_openai, \
         patch("backend.config.ALLOW_CLOUD_FALLBACK", False):

        with pytest.raises(RuntimeError, match="All LLM providers failed to generate a response"):
            _execute_generate_text(system_prompt="sys", user_prompt="usr")

        # OpenAI must NOT have been called because ALLOW_CLOUD_FALLBACK is False
        mock_openai.assert_not_called()

def test_queue_dynamic_concurrency_scaling():
    """Verify that scaling up workers allows multiple tasks to execute in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    mgr = LLMQueueManager(max_workers=4, pacing_seconds=0.0, mode="cloud")
    barrier = threading.Barrier(4)
    results = []

    def parallel_worker(idx):
        barrier.wait(timeout=2.0)
        results.append(idx)
        return idx

    with ThreadPoolExecutor(max_workers=4) as client_pool:
        futures = [
            client_pool.submit(mgr.submit, parallel_worker, i, timeout=3.0, task_name=f"task-{i}")
            for i in range(4)
        ]
        res = [f.result(timeout=3.0) for f in futures]

    assert len(results) == 4
    assert sorted(res) == [0, 1, 2, 3]
    status = mgr.get_status()
    assert status["max_concurrency"] == 4
    assert status["mode"] == "cloud"

def test_queue_scale_down_and_sync():
    """Verify scaling down and syncing with provider locality."""
    mgr = LLMQueueManager(max_workers=4, pacing_seconds=0.0, mode="cloud")
    mgr.sync_provider_concurrency(is_local=True)
    status = mgr.get_status()
    assert status["max_concurrency"] == 1
    assert status["mode"] == "local"

    mgr.sync_provider_concurrency(is_local=False)
    status2 = mgr.get_status()
    assert status2["max_concurrency"] == 8
    assert status2["mode"] == "cloud"



def test_scale_down_then_up_restores_the_full_pool():
    """Scaling cloud -> local -> cloud must give back cloud concurrency.

    The retired workers used to stay in the pool list, so the scale-up saw a full pool and
    spawned nothing: the queue ran with one worker permanently, and nothing reported it.
    """
    from concurrent.futures import ThreadPoolExecutor

    mgr = LLMQueueManager(max_workers=8, pacing_seconds=0.0, mode="cloud")
    mgr.sync_provider_concurrency(is_local=True)
    assert mgr.get_status()["max_concurrency"] == 1

    # Wait for the excess workers to actually exit, which is the state that used to be permanent.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if len([t for t in mgr._workers.values() if t.is_alive()]) <= 1:
            break
        time.sleep(0.05)
    assert len([t for t in mgr._workers.values() if t.is_alive()]) == 1

    mgr.sync_provider_concurrency(is_local=False)
    total = mgr.get_status()["max_concurrency"]
    assert total >= 4
    live = [t for t in mgr._workers.values() if t.is_alive()]
    assert len(live) == total, f"expected {total} live workers, got {len(live)}"

    # Prove it: four tasks only finish if four workers really run them.
    barrier = threading.Barrier(4)

    def needs_four(idx):
        barrier.wait(timeout=3.0)
        return idx

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(mgr.submit, needs_four, i, timeout=5.0, task_name=f"parallel-{i}")
            for i in range(4)
        ]
        assert sorted(f.result(timeout=6.0) for f in futures) == [0, 1, 2, 3]


def test_stop_fails_queued_futures_so_callers_do_not_wait_out_the_timeout():
    """A stopped queue must resolve pending work, not leave submit() blocked (finding 11)."""
    q = LLMQueueManager(max_workers=1)
    q.stop()

    task = LLMTask(lambda: 1, (), {}, priority=LLMPriority.ON_DEMAND, task_name="pending")
    q._queue.put(task)
    q.stop()

    with pytest.raises(RuntimeError, match="stopped"):
        task.future.result(timeout=1)


def test_submit_after_stop_fails_fast():
    q = LLMQueueManager(max_workers=1)
    q.stop()

    with pytest.raises(RuntimeError, match="stopped"):
        q.submit(lambda: 1, timeout=0.5)
