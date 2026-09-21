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

