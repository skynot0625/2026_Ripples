import torch
import torch.nn as nn
import numpy as np
import matplotlib.pyplot as plt
import time
import seaborn as sns

# Style Configuration
plt.style.use('default')
sns.set_palette("husl")
sns.set_style("whitegrid")

# ============================================================================
# === 0. 공통: Avalanche power-law fitting helper (통일된 피팅) ===
# ============================================================================

def fit_avalanches_power_law(sizes,
                             min_size=3,
                             max_tail_frac=0.85,
                             min_bins=8,
                             min_samples=300):
    """
    sizes: avalanche size 1D array
    return: gamma, r2, (fit_unique, fit_probs) or (0,0,None) if 실패
    """

    if sizes is None or len(sizes) < min_samples:
        return 0.0, 0.0, None

    unique, counts = np.unique(sizes, return_counts=True)
    probs = counts / counts.sum()

    # 최소 avalanche 크기 기준
    mask = unique >= min_size
    unique = unique[mask]
    probs = probs[mask]

    if len(unique) < min_bins:
        return 0.0, 0.0, None

    # tail cut (finite-size effect 완화)
    max_idx = int(len(unique) * max_tail_frac)
    unique = unique[:max_idx]
    probs = probs[:max_idx]

    if len(unique) < min_bins:
        return 0.0, 0.0, None

    x = np.log10(unique)
    y = np.log10(probs)

    # 선형 회귀 (로그-로그 공간)
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    gamma = -slope
    return float(gamma), float(r2), (unique, probs)

# ============================================================================
# === 1. Neuron Model (AdEx) ===
# ============================================================================

class AdExLIFNode(nn.Module):
    """Adaptive Exponential Integrate-and-Fire (AdEx) Model"""
    def __init__(self, N, dt=0.1,
                 tau_modes=None,
                 sfa_b_values=None,
                 ref_period=1.0,
                 noise_std=0.05,
                 device='cuda'):
        super().__init__()
        self.N = N
        self.dt = dt
        self.device = device

        if tau_modes is None:
            target_mean = 20.0
            sigma = 0.2
            mu = np.log(target_mean) - (sigma**2 / 2.0)
            mu_tensor = torch.tensor(mu, dtype=torch.float32, device=device)
            sigma_tensor = torch.tensor(sigma, dtype=torch.float32, device=device)
            dist = torch.distributions.LogNormal(mu_tensor, sigma_tensor)
            tau_m = dist.sample((N,))
        else:
            tau_m = torch.tensor(tau_modes, dtype=torch.float32, device=device)

        self.tau_m = torch.clamp(tau_m, min=5.0, max=100.0)
        self.decay_factor = torch.exp(-dt / self.tau_m)

        if sfa_b_values is None:
            self.b = torch.ones(N, dtype=torch.float32, device=device) * 10.0
        else:
            self.b = torch.tensor(sfa_b_values, dtype=torch.float32, device=device)

        self.a = torch.zeros(N, dtype=torch.float32, device=device)

        self.tau_w = self._sample_tau_w()
        self.decay_w = torch.exp(-dt / self.tau_w)

        self.ref_steps = int(ref_period / dt)
        self.noise_std = noise_std

    def _sample_tau_w(self):
        sigma = 0.4
        mode = 250.0
        mu = np.log(mode) + (sigma**2)

        mu_tensor = torch.tensor(mu, dtype=torch.float32, device=self.device)
        sigma_tensor = torch.tensor(sigma, dtype=torch.float32, device=self.device)
        dist = torch.distributions.LogNormal(mu_tensor, sigma_tensor)
        tau_w = dist.sample((self.N,))
        return torch.clamp(tau_w, min=50.0, max=2000.0)

    def forward(self, v, w, i_input, ref_timers):
        not_refractory = (ref_timers == 0)

        v[not_refractory] = (v[not_refractory] * self.decay_factor[not_refractory]
                             + i_input[not_refractory])

        if self.noise_std > 0:
            noise = torch.randn_like(v[not_refractory]) * self.noise_std
            v[not_refractory] += noise

        v[~not_refractory] = 0.0

        spikes = (v >= 1.0)

        if spikes.any():
            v[spikes] = 0.0
            ref_timers[spikes] = self.ref_steps

        ref_timers = torch.clamp(ref_timers - 1, min=0)

        w = w * self.decay_w + spikes.float() * self.b

        return v, w, spikes.float(), ref_timers

# ============================================================================
# === 2. Connectivity Builder ===
# ============================================================================

class EIConnectivityBuilder:
    """Generates spatially dependent E/I connectivity"""
    def __init__(self, N_E, N_I, dt=0.1, device='cuda'):
        self.N_E = N_E
        self.N_I = N_I
        self.N_total = N_E + N_I
        self.dt = dt
        self.device = device

        np.random.seed(42)
        self.coords_E = np.random.rand(N_E, 2)
        self.coords_I = np.random.rand(N_I, 2)

    def compute_distance(self, coords1, coords2):
        n1, n2 = coords1.shape[0], coords2.shape[0]
        dist = np.zeros((n1, n2))
        for i in range(n1):
            dist[i, :] = np.linalg.norm(coords1[i] - coords2, axis=1)
        return dist

    def build_connectivity(self,
                           lambda_E=0.25,
                           lambda_I=0.10,
                           p_rewire_E=0.08,
                           J_EE=1.0, J_EI=-1.0, J_IE=0.8, J_II=-0.8):
        W = np.zeros((self.N_total, self.N_total))

        # E->E
        d_EE = self.compute_distance(self.coords_E, self.coords_E)
        np.fill_diagonal(d_EE, np.inf)
        p_local_EE = np.exp(-d_EE / lambda_E)

        adj_EE = np.zeros((self.N_E, self.N_E))
        for i in range(self.N_E):
            candidates = np.where(np.random.rand(self.N_E) < p_local_EE[i])[0]
            candidates = candidates[candidates != i]

            if np.random.rand() < p_rewire_E and len(candidates) > 0:
                if len(candidates) > 1:
                    idx = np.random.choice(candidates)
                    candidates = np.delete(candidates, np.where(candidates == idx)[0])
                    random_idx = np.random.randint(0, self.N_E)
                    candidates = np.append(candidates, random_idx)

            adj_EE[i, candidates[:int(len(candidates)*0.3)]] = 1.0

        W[:self.N_E, :self.N_E] = adj_EE * J_EE

        # E->I
        d_EI = self.compute_distance(self.coords_I, self.coords_E)
        p_local_EI = np.exp(-d_EI / lambda_E)
        adj_EI = (np.random.rand(self.N_I, self.N_E) < p_local_EI).astype(np.float32)
        W[self.N_E:self.N_total, :self.N_E] = adj_EI * J_EI

        # I->E
        d_IE = self.compute_distance(self.coords_E, self.coords_I)
        p_local_IE = np.exp(-d_IE / lambda_I)
        adj_IE = (np.random.rand(self.N_E, self.N_I) < p_local_IE).astype(np.float32)
        W[:self.N_E, self.N_E:self.N_total] = adj_IE * J_IE

        # I->I
        d_II = self.compute_distance(self.coords_I, self.coords_I)
        np.fill_diagonal(d_II, np.inf)
        p_local_II = np.exp(-d_II / lambda_I)
        adj_II = (np.random.rand(self.N_I, self.N_I) < p_local_II).astype(np.float32)
        np.fill_diagonal(adj_II, 0)
        W[self.N_E:self.N_total, self.N_E:self.N_total] = adj_II * J_II

        # Spectral normalization
        spec_rad = np.max(np.abs(np.linalg.eigvals(W)))
        if spec_rad > 0:
            W /= spec_rad

        return W

def create_sfa_heterogeneity(N_E, device='cuda'):
    is_weak = np.random.rand(N_E) < 0.6
    n_weak = is_weak.sum()
    n_strong = N_E - n_weak

    tau_w_values = np.zeros(N_E)
    b_values = np.zeros(N_E)

    # --- Weak Group (60%): 빠른 회복 ---
    # 뉴런이 너무 오랫동안 잠들지 않게 하여 낮은 JE에서도 활동성을 유지하게 합니다.
    sigma_weak = 0.3
    mode_weak = 100.0  # 250 -> 100ms로 하향
    mu_weak = np.log(mode_weak) + (sigma_weak**2)
    dist_weak = torch.distributions.LogNormal(
        torch.tensor(mu_weak, device=device), 
        torch.tensor(sigma_weak, device=device)
    )
    tau_w_weak = dist_weak.sample((n_weak,)).cpu().numpy()
    tau_w_weak = np.clip(tau_w_weak, 30, 400) # 상한선 하향
    tau_w_values[is_weak] = tau_w_weak
    b_values[is_weak] = 0.05

    # --- Strong Group (40%): 안정화 기전 ---
    # 여전히 강력한 적응을 제공하지만, 회복 시간을 현실적으로 단축합니다.
    sigma_strong = 0.3
    mode_strong = 300.0 # 650 -> 300ms로 하향
    mu_strong = np.log(mode_strong) + (sigma_strong**2)
    dist_strong = torch.distributions.LogNormal(
        torch.tensor(mu_strong, device=device), 
        torch.tensor(sigma_strong, device=device)
    )
    tau_w_strong = dist_strong.sample((n_strong,)).cpu().numpy()
    tau_w_strong = np.clip(tau_w_strong, 100, 800) # 상한선 하향
    tau_w_values[~is_weak] = tau_w_strong
    b_values[~is_weak] = 0.15 # 0.2에서 약간 낮춤 (활동성 확보)

    return is_weak, tau_w_values, b_values

# ============================================================================
# === 3. E/I SOC Simulator ===
# ============================================================================

class EIAdaptiveSOC:
    """E/I network simulator (Fixed & Optimized)"""
    def __init__(self, N_E=800, N_I=200, dt=0.1, noise_std=0.05, device='cuda', seed=42):
        torch.manual_seed(seed)
        np.random.seed(seed)

        self.N_E = N_E
        self.N_I = N_I
        self.N_total = N_E + N_I
        self.dt = dt
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')

        print(f"\n{'='*60}")
        print(f"🧠 Building E/I Network (N_E={N_E}, N_I={N_I})")
        print(f"{'='*60}\n")

        self.is_weak_E, tau_w_E, b_E = create_sfa_heterogeneity(N_E, device=self.device)
        print(f"✓ SFA Assignment: {(self.is_weak_E).sum()} weak (60%), {(~self.is_weak_E).sum()} strong (40%)")

        print("\n📌 Creating neuron models...")

        self.neuron_E = AdExLIFNode(
            N_E, dt=dt,
            tau_modes=np.ones(N_E) * 20.0,
            sfa_b_values=b_E,
            ref_period=1.0,
            noise_std=noise_std,
            device=self.device
        )
        self.neuron_E.tau_w = torch.tensor(tau_w_E, dtype=torch.float32, device=self.device)
        self.neuron_E.decay_w = torch.exp(-dt / self.neuron_E.tau_w)

        self.neuron_I = AdExLIFNode(
            N_I, dt=dt,
            tau_modes=np.ones(N_I) * 5.0,
            sfa_b_values=np.ones(N_I) * 0.0,
            ref_period=1.0,
            noise_std=noise_std,
            device=self.device
        )

        print("\n⏱️  Setting up conduction delays...")
        target_delay_mean = 2.0
        delay_sigma = 0.5
        mu_d = np.log(target_delay_mean) - (delay_sigma**2 / 2.0)
        d_dist = torch.distributions.LogNormal(
            torch.tensor(mu_d, dtype=torch.float32, device=self.device),
            torch.tensor(delay_sigma, dtype=torch.float32, device=self.device)
        )

        delays_ms = d_dist.sample((self.N_total,))
        # min 값 교체
        delays_ms = torch.clamp(delays_ms, min=1.5, max=8.0)
        self.delay_steps = (delays_ms / dt).long()
        self.max_delay_steps = self.delay_steps.max().item()

        self.spike_buffer_E = torch.zeros((self.max_delay_steps + 1, N_E), device=self.device)
        self.spike_buffer_I = torch.zeros((self.max_delay_steps + 1, N_I), device=self.device)
        self.buffer_idx = 0

        print("\n🕸️  Building connectivity structure...")
        self.conn_builder = EIConnectivityBuilder(N_E, N_I, dt=dt, device=self.device)

        # Initial Weights
        self.set_weights(J_E_exc=0.5, J_I_inh_ratio=4.0)

        print(f"\n✅ Network initialization complete!")
        print(f"   Max delay: {self.max_delay_steps} steps ({self.max_delay_steps * dt:.2f} ms)")

    def set_weights(self, J_E_exc=0.5, J_I_inh_ratio=4.0):
        """
        Explicit weight setting (Biological I/E 4x principle)
        """
        J_EE = J_E_exc
        J_EI = J_E_exc
        J_IE = -J_E_exc * J_I_inh_ratio
        J_II = -J_E_exc * J_I_inh_ratio * 0.75

        W_full = self.conn_builder.build_connectivity(
            lambda_E=0.25,
            lambda_I=0.10,
            p_rewire_E=0.08,
            J_EE=J_EE,
            J_EI=J_EI,
            J_IE=J_IE,
            J_II=J_II
        )
        W_full = W_full * J_E_exc

        self.W_EE = torch.tensor(W_full[:self.N_E, :self.N_E], dtype=torch.float32, device=self.device)
        self.W_EI = torch.tensor(W_full[:self.N_E, self.N_E:], dtype=torch.float32, device=self.device)
        self.W_IE = torch.tensor(W_full[self.N_E:, :self.N_E], dtype=torch.float32, device=self.device)
        self.W_II = torch.tensor(W_full[self.N_E:, self.N_E:], dtype=torch.float32, device=self.device)

        self.gain_E = 1.0
        self.gain_I = 1.0

        self.J_E_exc = J_E_exc
        self.J_I_inh_ratio = J_I_inh_ratio

    def run_simulation(self, duration_ms=100000, record_raster=False):
        """
        Run simulation and record data
        """
        steps = int(duration_ms / self.dt)

        v_E = torch.zeros(self.N_E, device=self.device)
        v_I = torch.zeros(self.N_I, device=self.device)
        w_E = torch.zeros(self.N_E, device=self.device)
        w_I = torch.zeros(self.N_I, device=self.device)

        ref_E = torch.zeros(self.N_E, dtype=torch.long, device=self.device)
        ref_I = torch.zeros(self.N_I, dtype=torch.long, device=self.device)

        self.spike_buffer_E.fill_(0)
        self.spike_buffer_I.fill_(0)
        self.buffer_idx = 0

        # Raw Data Recording
        activity_E_history = []
        activity_I_history = []

        # Raster Data Recording (Time, Neuron Index)
        raster_E_times = []
        raster_E_indices = []
        raster_I_times = []
        raster_I_indices = []

        print(f"🚀 Simulation: {duration_ms}ms (Recording Raw Spikes...)")
        start_time = time.time()

        kick_cd = 0

        for t in range(steps):
            # 1. Fetch spikes from buffer
            arriving_E = self.spike_buffer_E[self.buffer_idx].clone()
            arriving_I = self.spike_buffer_I[self.buffer_idx].clone()

            self.spike_buffer_E[self.buffer_idx] = 0.0
            self.spike_buffer_I[self.buffer_idx] = 0.0

            # 2. Calculate Currents
            i_E_syn = (torch.matmul(self.W_EE, arriving_E) * self.gain_E
                       - torch.matmul(self.W_EI, arriving_I) * self.gain_I)
            i_I_syn = (torch.matmul(self.W_IE, arriving_E) * self.gain_E
                       - torch.matmul(self.W_II, arriving_I) * self.gain_I)

            i_E_adapt = -w_E * self.gain_E
            i_I_adapt = -w_I * self.gain_I

            i_E_total = i_E_syn + i_E_adapt
            i_I_total = i_I_syn + i_I_adapt

            # 3. External Kick (Keep activity alive)
            active_curr = arriving_E.sum().item() + arriving_I.sum().item()
            if active_curr == 0 and kick_cd <= 0:
                kick_idx = torch.randint(0, self.N_E, (1,), device=self.device)
                v_E[kick_idx] += 1.5
                kick_cd = 200  # 20ms cooldown
            if kick_cd > 0:
                kick_cd -= 1

            # 4. Update Neurons
            v_E, w_E, spikes_E, ref_E = self.neuron_E(v_E, w_E, i_E_total, ref_E)
            v_I, w_I, spikes_I, ref_I = self.neuron_I(v_I, w_I, i_I_total, ref_I)

            # 5. Propagate Spikes
            if spikes_E.any():
                spike_idx_E = torch.nonzero(spikes_E).squeeze()
                if spike_idx_E.dim() == 0:
                    spike_idx_E = spike_idx_E.unsqueeze(0)
                delays_E = self.delay_steps[:self.N_E][spike_idx_E]
                target_E = (self.buffer_idx + delays_E) % (self.max_delay_steps + 1)
                self.spike_buffer_E.index_put_(
                    (target_E, spike_idx_E),
                    torch.tensor(1.0, device=self.device),
                    accumulate=True
                )

            if spikes_I.any():
                spike_idx_I = torch.nonzero(spikes_I).squeeze()
                if spike_idx_I.dim() == 0:
                    spike_idx_I = spike_idx_I.unsqueeze(0)
                delays_I = self.delay_steps[self.N_E:][spike_idx_I]
                target_I = (self.buffer_idx + delays_I) % (self.max_delay_steps + 1)
                self.spike_buffer_I.index_put_(
                    (target_I, spike_idx_I),
                    torch.tensor(1.0, device=self.device),
                    accumulate=True
                )

            # 6. Record
            activity_E_history.append(spikes_E.sum().item())
            activity_I_history.append(spikes_I.sum().item())

            # 7. Record Raster (Optional)
            if record_raster:
                current_time = t * self.dt
                if spikes_E.any():
                    idxs = torch.nonzero(spikes_E).view(-1).cpu().numpy()
                    raster_E_times.append(np.full(len(idxs), current_time))
                    raster_E_indices.append(idxs)

                if spikes_I.any():
                    idxs = torch.nonzero(spikes_I).view(-1).cpu().numpy()
                    raster_I_times.append(np.full(len(idxs), current_time))
                    # Shift I indices by N_E for visualization
                    raster_I_indices.append(idxs + self.N_E)

            self.buffer_idx = (self.buffer_idx + 1) % (self.max_delay_steps + 1)

            if t % 500000 == 0 and t > 0:
                print(f"   ... {int(t*self.dt/1000)}s processed")

        elapsed = time.time() - start_time
        print(f"✅ Simulation complete! ({elapsed:.2f}s)")

        # Return Results
        act_E_arr = np.array(activity_E_history)
        act_I_arr = np.array(activity_I_history)

        if record_raster:
            if raster_E_times:
                r_E = np.column_stack((np.concatenate(raster_E_times),
                                       np.concatenate(raster_E_indices)))
            else:
                r_E = np.empty((0, 2))

            if raster_I_times:
                r_I = np.column_stack((np.concatenate(raster_I_times),
                                       np.concatenate(raster_I_indices)))
            else:
                r_I = np.empty((0, 2))

            return act_E_arr, act_I_arr, r_E, r_I

        return act_E_arr, act_I_arr

# ============================================================================
# === 4. Time Binning Analysis (Excitatory Only Option) ===
# ============================================================================

def analyze_binned_avalanches(activity_E, activity_I, dt=0.1,
                              bin_size_ms='adaptive',
                              threshold_ratio=0.1,
                              use_only_exc=True):
    """
    Time Binning + Thresholding (Adaptive Binning)
    """

    # 1. Select Data
    if use_only_exc:
        total_activity = activity_E
    else:
        total_activity = activity_E + activity_I

    # 2. Determine Bin Size
    if bin_size_ms == 'adaptive':
        mean_rate = np.mean(total_activity)
        if mean_rate > 0:
            avg_isi_steps = 1.0 / mean_rate
        else:
            avg_isi_steps = 1.0

        steps_per_bin = max(1, int(np.round(avg_isi_steps)))
    else:
        steps_per_bin = max(1, int(bin_size_ms / dt))

    # 3. Binning (Reshape & Sum)
    n_bins = len(total_activity) // steps_per_bin
    truncated_len = n_bins * steps_per_bin

    reshaped = total_activity[:truncated_len].reshape(n_bins, steps_per_bin)
    binned_activity = reshaped.sum(axis=1)

    # 4. Thresholding
    if threshold_ratio > 0:
        threshold = np.mean(binned_activity) * threshold_ratio
    else:
        threshold = 0.5

    # 5. Avalanche Detection
    is_active = (binned_activity > threshold).astype(int)

    avalanches = []
    current_size = 0

    for val, active in zip(binned_activity, is_active):
        if active:
            current_size += val
        else:
            if current_size > 0:
                avalanches.append(current_size)
                current_size = 0

    return np.array(avalanches), binned_activity

# ============================================================================
# === 5. Weight Sweep & Visualization Functions ===
# ============================================================================

def run_weight_sweep(J_E_values, J_I_ratio=4.0, duration_per_condition_ms=100000,
                     N_E=800, N_I=200):

    results = {
        'J_E_values': J_E_values,
        'gamma_values': [],
        'r2_values': [],
        'mean_rate_E': [],
        'mean_rate_I': [],
        'ei_ratio': [],
        'n_avalanches': [],
    }

    print(f"\n{'='*70}")
    print(f"🔍 Weight Sweep (Unified Fitting & Adaptive Binning - Exc Only)")
    print(f"{'='*70}\n")

    sim = EIAdaptiveSOC(N_E=N_E, N_I=N_I, device='cuda', seed=42)

    for idx, J_E in enumerate(J_E_values):
        print(f"[{idx+1}/{len(J_E_values)}] Testing J_E_exc = {J_E:.4f}")

        sim.set_weights(J_E_exc=J_E, J_I_inh_ratio=J_I_ratio)
        act_E, act_I = sim.run_simulation(duration_ms=duration_per_condition_ms)

        # Exc only
        sizes, binned_act = analyze_binned_avalanches(
            act_E, act_I,
            dt=sim.dt,
            bin_size_ms='adaptive',
            threshold_ratio=0.1,
            use_only_exc=True
        )

        gamma, r2, _ = fit_avalanches_power_law(
            sizes,
            min_size=3,
            max_tail_frac=0.85,
            min_bins=8,
            min_samples=300
        )

        mean_rate_E = act_E.mean()
        mean_rate_I = act_I.mean()
        ei_ratio_val = mean_rate_E / (mean_rate_I + 1e-6)

        results['gamma_values'].append(gamma)
        results['r2_values'].append(r2)
        results['mean_rate_E'].append(mean_rate_E)
        results['mean_rate_I'].append(mean_rate_I)
        results['ei_ratio'].append(ei_ratio_val)
        results['n_avalanches'].append(len(sizes))

        print(f"   → γ = {gamma:.4f} (R² = {r2:.4f}) | Avalanches: {len(sizes)}")

    for key in results:
        if isinstance(results[key], list):
            results[key] = np.array(results[key])

    return results

def plot_weight_sweep_results(results, target_gamma=1.5):
    """
    Visualize weight sweep results
    """
    J_E_values = results['J_E_values']
    gamma_values = results['gamma_values']
    ei_ratio = results['ei_ratio']
    mean_rate_E = results['mean_rate_E']
    mean_rate_I = results['mean_rate_I']

    fig = plt.figure(figsize=(14, 10))
    plt.suptitle("Weight Sweep (Exc Only Avalanche Analysis)", fontsize=16, fontweight='bold')

    # 1. γ vs J_E
    ax1 = fig.add_subplot(2, 2, 1)
    ax1.plot(J_E_values, gamma_values, 'o-', linewidth=2.5, markersize=8,
             color='steelblue', label='Measured γ')
    ax1.axhline(y=target_gamma, color='red', linestyle='--', linewidth=2.5,
                label=f'Target: γ = {target_gamma}')

    idx_best = np.argmin(np.abs(gamma_values - target_gamma))
    ax1.scatter(J_E_values[idx_best], gamma_values[idx_best],
                s=200, marker='*', color='gold', edgecolor='red', linewidth=2,
                label=f'Best: J_E = {J_E_values[idx_best]:.4f}', zorder=5)

    ax1.set_xlabel("J_E (Excitatory Weight Scale)", fontsize=11, fontweight='bold')
    ax1.set_ylabel("γ (Avalanche Exponent)", fontsize=11, fontweight='bold')
    ax1.set_title("Criticality vs Weight")
    ax1.legend(fontsize=10)
    ax1.grid(True, alpha=0.3)

    # 2. E/I ratio
    ax2 = fig.add_subplot(2, 2, 2)
    ax2.plot(J_E_values, ei_ratio, 's-', linewidth=2.5, markersize=8,
             color='darkviolet', label='E/I Ratio')
    ax2.axhline(y=4.0, color='green', linestyle='--', linewidth=2,
                label='Target: E/I = 4.0x')
    ax2.scatter(J_E_values[idx_best], ei_ratio[idx_best],
                s=200, marker='*', color='gold', edgecolor='red', linewidth=2, zorder=5)

    ax2.set_xlabel("J_E (Excitatory Weight Scale)", fontsize=11, fontweight='bold')
    ax2.set_ylabel("E/I Spike Ratio", fontsize=11, fontweight='bold')
    ax2.set_title("E/I Balance vs Weight")
    ax2.legend(fontsize=10)
    ax2.grid(True, alpha=0.3)

    # 3. Activity rates
    ax3 = fig.add_subplot(2, 2, 3)
    ax3.plot(J_E_values, mean_rate_E, 'o-', linewidth=2, markersize=7,
             color='blue', label='E rate')
    ax3.plot(J_E_values, mean_rate_I, 's-', linewidth=2, markersize=7,
             color='red', label='I rate')
    ax3.scatter(J_E_values[idx_best], mean_rate_E[idx_best],
                s=200, marker='*', color='gold', edgecolor='blue', linewidth=2, zorder=5)
    ax3.scatter(J_E_values[idx_best], mean_rate_I[idx_best],
                s=200, marker='*', color='gold', edgecolor='red', linewidth=2, zorder=5)

    ax3.set_xlabel("J_E (Excitatory Weight Scale)", fontsize=11, fontweight='bold')
    ax3.set_ylabel("Mean Rate (spikes/step)", fontsize=11, fontweight='bold')
    ax3.set_title("Population Activity")
    ax3.legend(fontsize=10)
    ax3.grid(True, alpha=0.3)

    # 4. Summary
    ax4 = fig.add_subplot(2, 2, 4)
    ax4.axis('off')

    best_J_E = J_E_values[idx_best]
    best_gamma = gamma_values[idx_best]
    best_ei = ei_ratio[idx_best]
    best_E_rate = mean_rate_E[idx_best]
    best_I_rate = mean_rate_I[idx_best]

    summary_text = f"""
╔════════════════════════════════════════════╗
║    🎯 RECOMMENDED CONFIGURATION            ║
╠════════════════════════════════════════════╣
║  J_E_exc = {best_J_E:.4f}                    ║
║  J_I_inh_ratio = 4.0 (biological)          ║
║  Analysis: Excitatory Only                 ║
║                                            ║
║  Results:                                  ║
║  • γ = {best_gamma:.4f} (target: {target_gamma})        ║
║  • E/I = {best_ei:.2f}x (target: 4.0x)        ║
║  • E rate = {best_E_rate:.2f} sp/step         ║
║  • I rate = {best_I_rate:.2f} sp/step         ║
║                                            ║
║  Status: {'✓ CRITICAL' if (target_gamma-0.1) < best_gamma < (target_gamma+0.1)
           else '⚠ NEAR-CRITICAL' if (target_gamma-0.3) < best_gamma < (target_gamma+0.3)
           else '✗ NON-CRITICAL'}  ║
╚════════════════════════════════════════════╝
    """

    ax4.text(0.1, 0.5, summary_text,
             transform=ax4.transAxes,
             fontsize=11, verticalalignment='center',
             family='monospace',
             bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.8))

    plt.tight_layout()
    plt.show()

    return best_J_E

def analyze_and_visualize(sim, activity_E, activity_I, raster_E, raster_I,
                          duration_ms, zoom_ms=20000):
    """
    Comprehensive visualization (Exc Only for Avalanche)
    """
    print("Analyzing data...")
    dt = sim.dt
    total_steps = int(duration_ms / dt)
    time_axis = np.linspace(0, duration_ms, total_steps)

    # ==================================================================
    # [수정됨] bin_size_ms를 4.0 -> 'adaptive'로 변경하여 Sweep과 조건 통일
    # ==================================================================
    sizes, binned_act = analyze_binned_avalanches(
        activity_E, activity_I, dt=dt, 
        bin_size_ms='adaptive',   # <--- 여기를 수정했습니다!
        threshold_ratio=0.1, 
        use_only_exc=True
    )

    # 2. Population activity
    all_activity = activity_E + activity_I
    inst_rate = all_activity / (sim.N_total * (dt / 1000.0))  # Hz

    window_ms = 50
    window_steps = int(window_ms / dt)
    if window_steps > 0:
        kernel = np.ones(window_steps) / window_steps
        avg_rate = np.convolve(inst_rate, kernel, mode='same')
    else:
        avg_rate = inst_rate

    # ==================== Visualization ====================
    fig = plt.figure(figsize=(16, 12))
    gs = fig.add_gridspec(3, 2, height_ratios=[1.2, 1, 1])

    # --- (1) Raster Plot ---
    ax1 = fig.add_subplot(gs[0, :])
    t_start_zoom = max(0, duration_ms - zoom_ms)

    if len(raster_E) > 0:
        mask_E = raster_E[:, 0] >= t_start_zoom
        ax1.scatter(raster_E[mask_E, 0], raster_E[mask_E, 1], s=2,
                    color='royalblue', label='Excitatory', alpha=0.6)

    if len(raster_I) > 0:
        mask_I = raster_I[:, 0] >= t_start_zoom
        ax1.scatter(raster_I[mask_I, 0], raster_I[mask_I, 1], s=2,
                    color='crimson', label='Inhibitory', alpha=0.6)

    ax1.set_xlim(t_start_zoom, duration_ms)
    ax1.set_ylim(-5, sim.N_total + 5)
    ax1.set_title(f"Raster Plot (E/I Network, Last {zoom_ms/1000:.1f}s)",
                  fontsize=14, fontweight='bold')
    ax1.set_ylabel("Neuron ID")
    ax1.legend(loc='upper right')

    # --- (2) Avalanche Size Distribution (Exc Only) ---
    ax2 = fig.add_subplot(gs[1:, 0])

    if len(sizes) > 0:
        unique, counts = np.unique(sizes, return_counts=True)
        probs = counts / counts.sum()

        ax2.loglog(unique, probs, 'o', color='purple', alpha=0.6, markersize=6,
                   markeredgecolor='white', label='Exc. Avalanches')

        gamma, r2, pack = fit_avalanches_power_law(
            sizes,
            min_size=3,
            max_tail_frac=0.85,
            min_bins=8,
            min_samples=300
        )

        if pack is not None:
            fit_x, fit_p = pack
            x_log = np.log10(fit_x)
            y_log = np.log10(fit_p)
            slope = -gamma
            intercept = np.mean(y_log - slope * x_log)
            x_line_log = np.linspace(x_log.min(), x_log.max(), 100)
            y_line_log = slope * x_line_log + intercept

            ax2.plot(10**x_line_log, 10**y_line_log,
                     linestyle='--', color='gray', linewidth=2.5, alpha=0.8,
                     label=f'Fit: γ = {gamma:.2f}, R²={r2:.2f}')

        ax2.set_title("Avalanche Size Distribution (Exc Only)", fontsize=14, fontweight='bold')
        ax2.set_xlabel("Avalanche Size (Spikes)")
        ax2.set_ylabel("Probability P(S)")
        ax2.legend(fontsize=10)
        ax2.grid(True, which="both", ls="--", alpha=0.3)
    else:
        ax2.text(0.5, 0.5, "No avalanches detected", ha='center')

    # --- (3) Population Firing Rate ---
    ax3 = fig.add_subplot(gs[1:, 1])

    idx_start = int(t_start_zoom / dt)
    zoom_time = time_axis[idx_start:]
    zoom_inst = inst_rate[idx_start:]
    zoom_avg = avg_rate[idx_start:]

    ax3.plot(zoom_time, zoom_inst, color='silver', alpha=0.6, label='Instantaneous')
    ax3.plot(zoom_time, zoom_avg, color='darkgreen', lw=2, label=f'Averaged ({window_ms}ms)')

    ax3.set_xlim(t_start_zoom, duration_ms)
    ax3.set_title("Total Population Firing Rate (Hz)", fontsize=14, fontweight='bold')
    ax3.set_xlabel("Time (ms)")
    ax3.set_ylabel("Rate (Hz)")
    ax3.legend(loc='upper right')
    ax3.grid(True, alpha=0.5)

    plt.tight_layout()
    plt.show()

# ============================================================================
# === Main ===
# ============================================================================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🔬 E/I SFA Network - Comprehensive Visualization (Exc Only Analysis)")
    print("="*70)

    # 1. Simulator Initialization
    sim = EIAdaptiveSOC(N_E=800, N_I=200, dt=0.1, device='cuda')

    # 2. Weight Sweep Analysis (duration 늘림)
    sweep_results = run_weight_sweep(
        J_E_values=np.linspace(1.0, 5.0, 9),
        duration_per_condition_ms=100000   # 50,000 -> 100,000 ms
    )
    best_J = plot_weight_sweep_results(sweep_results)

    # 3. Single Run Configuration
    sim.set_weights(J_E_exc=best_J, J_I_inh_ratio=4.0)

    # 4. Run Single Simulation (duration 맞춰서 길게)
    duration = 100000  # ms
    print(f"Running single simulation for {duration}ms...")
    act_E, act_I, r_E, r_I = sim.run_simulation(duration_ms=duration, record_raster=True)

    # 5. Visualize Single Run
    analyze_and_visualize(sim, act_E, act_I, r_E, r_I, duration, zoom_ms=20000)
