---
title: Demystifying Kubernetes Scheduler — Part II: Scheduling Queue
date: 2025-08-16
summary: Let’s explore the inner workings of Kubernetes’s Scheduling Queue — from ActiveQ to BackoffQ to UnschedulablePods — and see how its design keeps scheduling efficient and resilient.
---

Inside Kubernetes, the Scheduling Queue is like a train station with multiple waiting areas — every Pod that needs a seat on a node must pass through it first.

But this isn’t a simple first-come, first-served line. It’s a tiered, dynamic system where Pods are managed according to their scheduling readiness:

- Some are ready to board right away (**ActiveQ**).
- Some clearly can’t get a seat yet and must wait for conditions to change (**UnschedulablePods**).
- Others have just been denied and need to “cool off” before trying again (**BackoffQ**).

This design tackles a critical challenge: how to keep scheduling efficient when thousands of Pods are competing for placement, while avoiding wasting CPU cycles on attempts that are guaranteed to fail in the short term.

## Pod flow through the queues

The SchedulingQueue defines how a Pod moves between its sub-queues until it is successfully scheduled.

1. **New Pod**
   - When a Pod is created (or observed as unscheduled at startup), it enters **ActiveQ** immediately.
   - Only Pods in ActiveQ are popped by the scheduler and considered for placement.
2. **Scheduling Attempt**
   - The scheduler pops a Pod from ActiveQ and runs the scheduling cycle.
   - If scheduling succeeds, the Pod is bound to a Node and leaves the queue permanently.
   - If scheduling fails because of **no feasible node**, the Pod is placed into **UnschedulablePods**.
   - If scheduling fails due to **transient or internal errors** (e.g., Reserve, Permit, PreBind, or Bind failures), the Pod is requeued with **backoff** into **BackoffQ**.
3. **UnschedulablePods**
   - Pods here are “paused” — they will not be retried until conditions change.
   - Two mechanisms can move Pods out:
     - **Event-driven wake-up**: a cluster change (e.g., Node added, PVC bound, Pod terminated) triggers a targeted **MoveRequest**.
     - **Periodic flush**: every 30s, Pods that stayed longer than `PodMaxInUnschedulablePodsDuration` (default 60s) are retried.
4. **BackoffQ**
   - The scheduler runs `flushBackoffQCompleted` every second, moving expired Pods back into **ActiveQ**.
   - If the backoff timer has not expired, the Pod is placed into **BackoffQ** (min-heap on `readyTime`)
   - When an Unschedulable Pod is retried, it may still be throttled by backoff.
5. **Retry and Success**
   - A Pod may bounce between UnschedulablePods and BackoffQ several times.
   - Eventually, when resources free up or new Nodes arrive, the Pod returns to ActiveQ, is scheduled successfully, and leaves the queue.

## **Queue Data Structure**

At the core of Kubernetes’s scheduling queues is a [**keyed heap**](https://github.com/kubernetes/kubernetes/blob/v1.33.3/pkg/scheduler/backend/heap/heap.go#L133) — a custom extension of Go’s `container/heap`.
Unlike a plain heap, the keyed heap maintains an additional [**`items` map**](https://github.com/kubernetes/kubernetes/blob/v1.33.3/pkg/scheduler/backend/heap/heap.go#L48) (`key → heapItem`), where each `heapItem` stores the object and its current index in the heap array.

This design enables:

- **O(1) lookups**: `GetByKey` returns the requested item without scanning the heap.
- **O(log n) updates**: `AddOrUpdate` inserts a new item with `heap.Push`, or updates an existing one with `heap.Fix`.
- **O(log n) deletions**: `Delete` removes an item via `heap.Remove`
- **Strict uniqueness**: each key can appear at most once; updates never create duplicates.

The heap updates each item’s `index` on every `Swap/Push/Pop/Remove`, and the `items` map is updated on insert/delete, keeping the map and heap array consistent.


### **ActiveQ**

A **max-heap** keyed by Pod priority (FIFO within the same priority).
Built directly on the keyed heap with a comparator that sorts by `priority` → `enqueueTime`.
Used for Pods that are immediately schedulable.


### **BackoffQ**

A **min-heap** keyed by the Pod’s `readyTime` (when backoff expires).
Same keyed heap structure, different comparator.
Used to hold Pods temporarily throttled by exponential backoff.


### **UnschedulablePods**

A simple `map[key] → PodInfo`, no ordering.
Pods stay here until an event or timeout makes them eligible again, at which point they are re-inserted into ActiveQ or BackoffQ via the keyed heap.

## Failure & Recovery

We notice that the **SchedulingQueue** is purely in-memory, which means all of its data is lost when the scheduler restarts or fails over. However, a Pod’s actual state (`Pending`, `Running`, `PodScheduled` condition, etc.) is stored in the **kube-apiserver** / **etcd**, not in the scheduler’s local memory. Therefore, restarting the scheduler does not cause Pods to disappear; the queue can be rebuilt from the latest cluster state. This design favors simplicity and responsiveness over persisting queue state — at the cost of losing backoff timers and possibly retrying some Pods sooner than intended.

When a new scheduler instance takes over, it performs a full sync of all `Pending` Pods from the API Server and rebuilds the queues. Technically, all Pods are inserted into **ActiveQ**; backoff and unschedulable state information is not persisted. This may cause a short burst of repeated scheduling attempts for Pods that are still impossible to place, but they will quickly return to the appropriate queue.

In short, a failover simply triggers a one-time full scan, which does not change the final scheduling results and only causes a brief scheduling jitter at most.

## **At the End**

Kubernetes’s scheduling queue is not just a waiting line — it is a carefully engineered control system. By separating Pods into **ActiveQ**, **BackoffQ**, and **UnschedulablePods**, the scheduler avoids wasting cycles on hopeless attempts while still reacting quickly when the cluster changes. The keyed heap underneath provides both fast lookups and strict ordering, ensuring fairness and efficiency at scale. Even when the scheduler itself fails over, the design leans on the **kube-apiserver** as the single source of truth, allowing the queue to be rebuilt seamlessly.