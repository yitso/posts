## From Filtering to Scoring: Feasible Nodes

In Kubernetes scheduling, once the **filtering** phase narrows the cluster down to a set of **feasible nodes** for a Pod, the scheduler enters the **scoring** phase. Scoring is applied only to those nodes that passed all filters. To reduce overhead in very large clusters, the scheduler may sample only a subset of nodes for both filtering and scoring, controlled by the `percentageOfNodesToScore` parameter.

The key distinction is that filtering answers *"Which nodes can run this Pod?"* while scoring answers *"Which of those nodes is the best candidate?"*

## Score Plugin Architecture: Extension Points and Execution Flow

**Score plugins** implement the `ScorePlugin` interface defined in (`pkg/scheduler/framework/interface.go`)[https://github.com/kubernetes/kubernetes/blob/v1.33.4/pkg/scheduler/framework/interface.go#L607] and execute through a structured multi-phase process. Scoring expresses *preferences*, not hard requirements. A node with a low score remains eligible as long as it passed filtering. If filtering produces no feasible nodes, the scheduler cannot proceed to scoring and instead invokes the **PostFilter** extension point, which may include preemption strategies.

### PreScore Extension Point

Before per-node evaluation begins, plugins may implement an optional **PreScore** hook. This phase runs once per scheduling cycle to prepare shared data structures, avoiding expensive recomputation for each node:

- **PodTopologySpread** counts existing Pods across zones/nodes during PreScore
- **InterPodAffinity** precomputes matching Pod counts per domain for efficient affinity calculations
- **TaintToleration** preprocesses Pod tolerations to simplify scoring logic

PreScore failure aborts the scheduling cycle for that Pod.

### Score Extension Point

Each plugin's `Score()` method evaluates every feasible node in parallel (bounded by internal worker pools). **Plugins return integer scores within the range `[0, MaxNodeScore]` (typically 0–100)**, representing how well each node satisfies the plugin's specific preference.

### NormalizeScore Extension Point

Plugins may optionally implement `NormalizeScore()` to adjust raw scores across all nodes in the cycle. **This method must return values within `[0, MaxNodeScore]` or the scheduler will abort with an error**. This phase handles scaling, inversion, or normalization relative to min/max values to ensure score comparability across different plugins.

### Final Aggregation

The scheduler applies configured plugin weights and computes the final score through weighted summation:

```
for _, plugin := range plugins {
    nodeScore[node] += plugin.Score(node) * plugin.Weight
}
```

Plugin normalized scores respect the MaxNodeScore constraint, but total sums are unbounded. The node with the highest weighted total is selected, with random tie-breaking for identical scores.

## Score Plugin Examples

| Plugin                              | Optimization Target                 | Trade-off / Risk                                          |
| ----------------------------------- | ----------------------------------- | --------------------------------------------------------- |
| **NodeResourcesLeastAllocated**     | Distribute to less-utilized nodes   | Resource fragmentation, reduced bin-packing efficiency    |
| **NodeResourcesBalancedAllocation** | Maintain balanced CPU/Memory ratios | Node-local optimization ignores cluster-wide patterns     |
| **NodeAffinity (Preferred)**        | Match preferred node labels         | High weights can overwhelm other scoring factors          |
| **ImageLocality**                   | Leverage cached container images    | Creates sticky placement patterns, minimal default impact |

### NodeResourcesLeastAllocated

Assigns higher scores to nodes with greater available CPU/Memory capacity. Implements workload distribution but reduces bin-packing efficiency through resource fragmentation across partially-utilized nodes.

### NodeResourcesBalancedAllocation

Calculates score as `(1 - |cpuUtilization - memoryUtilization|) * MaxNodeScore`. Optimizes individual node resource ratios but may concentrate workloads on nodes with similar CPU/Memory usage patterns.

### NodeAffinity (Preferred)

Processes `preferredDuringSchedulingIgnoredDuringExecution` rules, accumulating points when nodes match specified labels. Preference weights (1–100) operate independently of the plugin's scheduler weight configuration.

### ImageLocality

Scores nodes based on cached image bytes, reducing container startup latency. Default weight configuration minimizes impact, but can reinforce scheduling patterns for image-heavy workloads.

## Weight Configuration and Score Combination

With individual plugin behaviors in mind, the next step is to understand how their outputs are aggregated into a single decision.

**Default plugin weights** (defined in `pkg/scheduler/framework/plugins/*/defaults.go`):

- `TaintToleration`: 3 (strongly penalize tainted nodes)
- `InterPodAffinity`: 2
- `NodeResourcesLeastAllocated`: 1
- `NodeResourcesBalancedAllocation`: 1
- `NodeAffinity`: 1
- `ImageLocality`: 1

**Weight behavior characteristics:**

- Zero weight disables plugin influence entirely
- **Weights operate as linear multipliers with extreme values effectively overriding all other considerations**
- No cross-plugin normalization occurs — raw weighted sum determines final ranking

## Tie-Breaking Mechanism

When multiple nodes achieve identical final scores, the scheduler uses **reservoir sampling** for unbiased random selection:

```go
bestScore = -1
count = 0
for node in nodes {
    if score[node] > bestScore {
        bestScore = score[node]
        chosen = node
        count = 1
    } else if score[node] == bestScore {
        count++
        if rand.Intn(count) == 0 {
            chosen = node
        }
    }
}
```

This mechanism prevents systematic bias toward specific node orderings and explains distribution variance among identical Pod workloads.

## Extending Scoring Capabilities

While the built-in plugins cover most common scheduling requirements, production environments often need custom scoring logic. Kubernetes provides two mechanisms to extend scheduling beyond the built-in plugins, each with distinct trade-offs.

### Scheduler Extenders (Legacy)

Before the **Scheduling Framework** was introduced, the only way to extend scheduling was through **Extenders**:

**Architecture:**

- An extender is an **external HTTP service** that the scheduler calls during scheduling
- Must implement endpoints like `/filter` (to remove nodes) and `/prioritize` (to return node scores)
- Historically, `/prioritize` returned scores in the range `0–10`; these are later scaled to `0–100` and combined with in-tree plugin scores via a configured weight

**Limitations:**

- Every scheduling decision triggers synchronous RPC calls, adding latency in the hot path
- External services create **single points of failure**; if the extender is slow or unavailable, scheduling performance degrades
- No access to scheduler framework optimizations like PreScore data sharing

Extenders remain supported for backward compatibility and appear in older clusters or vendor-specific solutions (e.g., early GPU scheduling implementations). However, they are **not the recommended approach** for new development.

### Out-of-tree Plugins (Recommended)

Since Kubernetes 1.19, the **Scheduling Framework** provides a first-class extension mechanism. Instead of RPC calls, **out-of-tree plugins** implement the same interfaces as built-in plugins (`ScorePlugin`, `FilterPlugin`, etc.):

**Advantages:**

- Plugins run **in-process** with the scheduler, eliminating network overhead and external dependencies
- Direct integration with framework extension points (`PreScore`, `Score`, `NormalizeScore`)
- Access to the same performance optimizations available to built-in plugins
- Configuration through standard scheduler profiles alongside built-in plugins

**Community ecosystem:** The `kubernetes-sigs/scheduler-plugins` repository maintains production-ready plugins including Coscheduling, CapacityScheduling, and NodeResourceTopology.

**Implementation path:** Custom plugins follow the same development patterns as built-in plugins, with access to scheduler framework APIs for node/pod information and caching mechanisms.

**Recommendation:** Use **framework plugins** for new features or custom logic. They provide better performance, reliability, and integration compared to legacy extenders.

## Performance Considerations and Observability

Scheduling performance often becomes a limiting factor in very large clusters. The scoring stage in particular can dominate CPU time when many nodes are feasible and multiple heavy plugins are enabled.

### Complexity and Hot Paths

**Time complexity:** Roughly `O(feasibleNodes × enabledScorePlugins)`. For a 5,000-node cluster with 10 active scoring plugins, that represents 50,000 scoring calls per Pod.

**Memory pressure:** Some plugins (e.g., InterPodAffinity, PodTopologySpread) maintain global maps of pod distribution; their PreScore step may consume significant memory as cluster size grows.

**Cache efficiency:** Plugins that repeatedly traverse node/pod objects without caching can trigger high GC pressure in Go runtime, particularly with naive custom plugin implementations.

### Common Bottlenecks

**InterPodAffinity / Anti-affinity:** Requires scanning many existing Pods across nodes or zones. In clusters with tens of thousands of Pods, this becomes one of the heaviest plugins by execution time.

**Custom ScorePlugins:** Plugins that query external APIs or perform complex computations (e.g., GPU bin-packing with NUMA awareness) can add milliseconds per node, which scales poorly across large feasible sets.

**Large feasible sets:** In permissive clusters with few active filters, almost every node becomes feasible, causing scoring to dominate the scheduling cycle duration.

### Observability and Debugging

**Verbose logs:** Running the scheduler with `--v=10` shows per-plugin scores for each node. This is the primary tool for debugging placement decisions.

**Trace logging:** With `--v=5+`, timing information for each scheduling phase becomes visible in logs.

**Prometheus metrics:**

- `scheduler_plugin_execution_duration_seconds` — per-plugin timing, broken down by extension point
- `scheduler_scheduling_duration_seconds` — end-to-end scheduling latency distribution
- `scheduler_pending_pods` — backlog of unscheduled Pods, an indirect signal of scheduler saturation

**Offline simulation:** The `scheduler-plugins/simulator` project allows replaying real workloads with modified plugin sets or weights without impacting production clusters.

### Mitigation Strategies

**Tune sampling:** Adjust `percentageOfNodesToScore` (default 50%). In large homogeneous clusters, lowering this to 10–20% reduces scheduling CPU usage drastically with minimal impact on placement quality.

**Prune plugin set:** Disable unused scoring plugins in scheduler profiles. Every plugin adds computational cost even if its influence on final decisions is marginal.

**Optimize heavy plugins:**

- For InterPodAffinity, restrict label scopes and namespaces to reduce the number of Pods requiring evaluation
- For topology spread, scope constraints to zones instead of nodes when placement requirements permit
- For custom plugins, avoid synchronous network calls and pre-cache static data

**Run multiple schedulers:** In extreme-scale clusters, deploy multiple scheduler instances with disjoint responsibilities (e.g., critical workloads vs. batch jobs) to reduce contention on individual scheduler threads.

### Practical Example

In a 3,000-node cluster:

- With default settings (10 plugins, `percentageOfNodesToScore=50%`), average scheduling latency measured ~150ms per Pod
- Reducing `percentageOfNodesToScore` to 20% and disabling `ImageLocality` reduced latency to ~60ms
- Scoping `InterPodAffinity` to namespace-level only provided an additional 25% latency reduction with negligible change in placement quality

## Common Pitfalls and Anti-Patterns

Armed with performance insights and observability tools, let's examine common pitfalls engineers encounter when tuning scoring:

**Overweighted Anti-Affinity** Setting `preferredDuringScheduling` weights to 100 does not guarantee spreading. High weights distort scoring behavior and degrade `InterPodAffinity` performance. Use **PodTopologySpread** or required anti-affinity for strict distribution requirements.

**Extreme Plugin Weights** Weights significantly larger than others create effective overrides. Reserve for genuine hard preferences such as regulatory compliance requirements.

**Misclassifying Requirements vs Preferences** Mandatory placement constraints belong in **filters** (nodeSelector, required affinity, taints), not high-weight scoring preferences.

**Performance Neglect** Each enabled plugin executes per feasible node. Monitor plugin execution metrics and disable unused plugins. Anti-affinity and scheduler extenders impose the highest computational overhead.

## In the End

Scoring in Kubernetes is straightforward in code — *per-node plugin scores, weighted sum, random tie-break*. But the engineering consequences are subtle:

- **Weights** are powerful levers; misuse (e.g. overweight anti-affinity) often hurts efficiency
- **Tie-breaking** is intentionally random, so don't expect deterministic placement when scores tie
- **Extensibility** is best done with framework plugins; extenders are legacy and add latency
- **Performance** matters: every plugin runs on every feasible node; watch logs and metrics to keep scheduling fast

The takeaway for engineers: don't treat scores as black magic. Understand the mechanics well enough to predict outcomes, use observability to validate assumptions, and only adjust weights or add plugins when you know the trade-offs. With that foundation, you can reason about placement decisions and even extend the scheduler to encode your own business logic.