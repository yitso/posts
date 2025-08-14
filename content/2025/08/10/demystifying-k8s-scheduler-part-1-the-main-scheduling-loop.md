---
title: Demystifying Kubernetes Scheduler — Part I: The Main Scheduling Loop
date: 2025-08-10
summary: The Kubernetes Scheduler decides where every Pod will run, but the inner workings are often hidden from view. I’ll break down how the scheduler selects nodes, handles failures, and retries scheduling, providing a clear view of this critical component in action.
---

The Kubernetes Scheduler is the central brain of the cluster, responsible for placing every Pod onto a suitable node. While the official documentation provides a solid foundation, it rarely delves into the intricate technical details of how the scheduler actually operates. 

I’ve long been curious about what happens inside this critical component, so I decided to dig deep and share what I’ve learned. In this first part, we’ll peel back the layers together and explore the scheduler’s core control loop — the engine that drives everything from handling new Pods to managing failures and retries.

## Scheduler’s Entry Point

Before scheduling can happen, the scheduler needs a source of work — the **SchedulingQueue**, an internal priority queue that holds all pending Pods. When a new Pod appears or an unschedulable Pod becomes eligible again, it’s placed into this queue.

The scheduler’s entry point is `Scheduler.Run()`, which starts the queue’s internal loop and continuously invokes the core scheduling function `scheduleOne`:

```go
sched.SchedulingQueue.Run(logger)
go wait.UntilWithContext(ctx, sched.ScheduleOne, 0)
```

## Scheduler’s Main Scheduling Loop: [`scheduleOne`](https://github.com/kubernetes/kubernetes/blob/v1.33.3/pkg/scheduler/schedule_one.go#L65)

At the heart of the Kubernetes scheduler is a control loop that pulls Pods from the internal queue and assigns each to a suitable Node. In the current design, this loop runs serially in a **single scheduling thread**, processing one Pod at a time. 

Each iteration of `scheduleOne` performs:

1. **Fetch the next pending Pod**: It calls `sched.NextPod()` to retrieve the highest-priority Pod from the scheduling queue. This call will block until a Pod is available for scheduling, avoiding busy-waiting on an empty queue. 
2. **Skip or proceed based on Pod state**: If the Pod was deleted or already scheduled by another scheduler, the loop may skip scheduling it.
3. **Run the scheduling cycle**: For a Pod that needs scheduling, the scheduler enters the **Scheduling Cycle**. This involves invoking the configured scheduling plugins in order (`PreFilter`, `Filter`, `PostFilter`, etc.) to determine a fit for the Pod. Essentially, the scheduler filters out nodes that don’t meet the Pod’s requirements (e.g. insufficient resources, missing labels, taints/tolerations) and then ranks the feasible nodes by scoring plugins. This process is synchronous per Pod – by design only one Pod at a time goes through **filtering** + **scoring** in a given scheduling thread. If a suitable node is found, the scheduler will reserve the Pod to that node (an **Assume** operation in cache) and prepare for binding. If no node is found, the scheduling fails for now (we’ll discuss how that Pod is requeued shortly).
4. **Launch the binding cycle**: After a successful scheduling decision, the scheduler triggers an asynchronous Binding Cycle. It spawns a goroutine to perform the binding, which updates the Kubernetes API to assign the Pod to the chosen Node. This asynchronous step allows the main scheduling loop to quickly move on to schedule other Pods without waiting for network/API latency. The binding cycle will confirm the assignment (and emit a “`Scheduled`” event) or handle errors (e.g. if the API call fails).
5. **Handle scheduling failure**: If the scheduling cycle could not find any suitable node (or if some error occurred), the scheduler invokes its failure handler logic. This records a FailedScheduling event for the Pod and requeues the Pod internally for a later attempt. Crucially, the Pod is not retried in a tight loop; instead, it’s placed into a specific holding queue (unschedulable/backoff) so that other pending Pods can be scheduled and this Pod will be retried after some conditions change or a backoff period expires.

## Scheduler’s Serialization and Parallelization

### Parallelism parameter

We mentioned previously, the core loop is serial: one Pod at a time. However, the scheduler has a configurable parameter [`parallelism`](https://kubernetes.io/docs/reference/config-api/kube-scheduler-config.v1/#kubescheduler-config-k8s-io-v1-KubeSchedulerConfiguration):

> *Parallelism defines the amount of parallelism in algorithms for scheduling Pods. Must be greater than 0. Defaults to 16.*

Actually, the `parallelism` parameter controls how many worker threads the scheduler uses when evaluating candidate nodes during the **Filter** and **Score** phases of a single Pod’s scheduling cycle.  

In other words, `parallelism=16` does **not** mean 16 Pods are scheduled at once; it means a single Pod’s node evaluation can run with up to 16 concurrent workers, and binding is decoupled from scheduling to overlap work.

### Why Single-Threaded?

Based on the current code and comments, the design seems aimed at keeping the core scheduling loop simple and predictable, avoiding the complexity of multi-Pod concurrency. Heavy operations are parallelized (node filtering/scoring) or made asynchronous (binding), so the loop itself stays lightweight.

### High Availability in the Scheduler

Because the scheduling loop is single-threaded per scheduler instance, a single instance would be a single point of failure. To avoid this, Kubernetes supports running multiple scheduler instances with **leader election** enabled. All instances watch the same cluster state, but only the leader actively schedules Pods, while the others remain in standby. If the leader fails, another instance quickly takes over. This keeps the scheduler stateless, easy to replace, and resilient to failures, ensuring uninterrupted scheduling service.

## At the End 

In this part, we looked inside the scheduler’s core control loop — from pulling Pods off the queue, through filtering and scoring nodes with configurable parallelism, to asynchronous binding and retry handling. We also saw why the loop remains single-threaded and how Kubernetes achieves high availability through leader election.
