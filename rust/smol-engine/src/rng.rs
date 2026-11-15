//! Random streams (Model Semantics §6) and the distributions of the traffic
//! patterns.
//!
//! - **Streams.** Every traffic statement has a stable source id,
//!   `"<kind>:<from>-><to>#<k>"`, and its generator is seeded from the first
//!   8 bytes, big-endian, of SHA-256 of `"<run seed>:<source id>"`, exactly
//!   as `smolpy.model.streams`.
//! - **Generator.** The engine's own: xoshiro256** seeded through SplitMix64.
//!   Its draws differ from SimPy's `random.Random`, so stochastic scenarios
//!   are compared statistically (ADR-0009).
//! - **Distributions** use the formulas of Python's `random` module, so the
//!   two engines draw from the same distributions.

use sha2::{Digest, Sha256};

/// The stream seed of a traffic statement.
pub fn stream_seed(run_seed: u64, source_id: &str) -> u64 {
    let digest = Sha256::digest(format!("{run_seed}:{source_id}").as_bytes());
    u64::from_be_bytes(digest[..8].try_into().unwrap())
}

/// xoshiro256** (Blackman and Vigna), seeded through SplitMix64.
pub struct Rng {
    s: [u64; 4],
}

impl Rng {
    pub fn new(seed: u64) -> Rng {
        let mut x = seed;
        let mut next = || {
            x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = x;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        };
        Rng {
            s: [next(), next(), next(), next()],
        }
    }

    fn next_u64(&mut self) -> u64 {
        let s = &mut self.s;
        let result = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = s[1] << 17;
        s[2] ^= s[0];
        s[3] ^= s[1];
        s[1] ^= s[2];
        s[0] ^= s[3];
        s[2] ^= t;
        s[3] = s[3].rotate_left(45);
        result
    }

    /// Uniform on [0, 1), with 53 random bits as `random.random()`.
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// `random.expovariate(lambd)`: `-log(1 - random()) / lambd`.
    pub fn expovariate(&mut self, lambd: f64) -> f64 {
        -(1.0 - self.random()).ln() / lambd
    }

    /// `random.paretovariate(alpha)`: `(1 - random()) ** (-1 / alpha)`.
    pub fn paretovariate(&mut self, alpha: f64) -> f64 {
        (1.0 - self.random()).powf(-1.0 / alpha)
    }

    /// `random.uniform(a, b)`: `a + (b - a) * random()`.
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }
}

/// Group sizes of the `batch` pattern, as `smolpy.core.batch.BatchSpec`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Batch {
    Fixed(u64),
    Geom(f64),
    Pareto(f64),
}

impl Batch {
    /// E[X], with the same zeta approximation as the reference engine.
    pub fn mean(self) -> f64 {
        match self {
            Batch::Fixed(k) => k as f64,
            Batch::Geom(m) => m,
            Batch::Pareto(a) => zeta(a),
        }
    }

    pub fn draw(self, rng: &mut Rng) -> u64 {
        match self {
            Batch::Fixed(k) => k,
            Batch::Geom(m) => {
                let p = 1.0 / m;
                if p >= 1.0 {
                    return 1;
                }
                // inverse transform for the geometric distribution on {1, 2, ...}
                let u = 1.0 - rng.random(); // in (0, 1]
                1 + (u.ln() / (1.0 - p).ln()) as u64
            }
            Batch::Pareto(a) => (rng.paretovariate(a) as u64).max(1),
        }
    }
}

/// Riemann zeta(s) for s > 1: direct sum plus an Euler-Maclaurin tail, as
/// `smolpy.core.batch._zeta` (whose sum is exact; this one is compensated).
pub fn zeta(s: f64) -> f64 {
    let terms = 10_000;
    let n = terms as f64;
    let (mut sum, mut c) = (0.0f64, 0.0f64);
    for k in 1..terms {
        let y = (k as f64).powf(-s) - c;
        let t = sum + y;
        c = (t - sum) - y;
        sum = t;
    }
    let tail = n.powf(1.0 - s) / (s - 1.0) + 0.5 * n.powf(-s) + s * n.powf(-s - 1.0) / 12.0;
    sum + tail
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stream_seed_is_a_sha256_prefix() {
        // the value of Model Semantics §6 and tests/test_semantics.py
        assert_eq!(stream_seed(42, "flow:a->b#0"), 5_297_917_812_165_586_412);
    }

    #[test]
    fn a_seed_gives_the_same_draws() {
        let (mut a, mut b) = (Rng::new(7), Rng::new(7));
        for _ in 0..1000 {
            assert_eq!(a.random().to_bits(), b.random().to_bits());
        }
        assert_ne!(Rng::new(7).random(), Rng::new(8).random());
    }

    fn mean(n: usize, mut f: impl FnMut() -> f64) -> f64 {
        (0..n).map(|_| f()).sum::<f64>() / n as f64
    }

    #[test]
    fn distributions_have_their_means() {
        let mut r = Rng::new(1);
        let n = 400_000;
        let u = mean(n, || r.random());
        assert!((u - 0.5).abs() < 0.003, "{u}");
        assert!((0..n).all(|_| (0.0..1.0).contains(&r.random())));
        let e = mean(n, || r.expovariate(1.0 / 20.0));
        assert!((e / 20.0 - 1.0).abs() < 0.01, "{e}");
        let p = mean(n, || r.paretovariate(3.0)); // mean a / (a - 1) = 1.5
        assert!((p / 1.5 - 1.0).abs() < 0.01, "{p}");
        let un = mean(n, || r.uniform(0.3, 1.2));
        assert!((un / 0.75 - 1.0).abs() < 0.005, "{un}");
        for batch in [Batch::Fixed(8), Batch::Geom(4.0), Batch::Pareto(2.5)] {
            let m = mean(n, || batch.draw(&mut r) as f64);
            assert!((m / batch.mean() - 1.0).abs() < 0.02, "{batch:?}: {m}");
        }
    }

    #[test]
    fn zeta_values() {
        assert!((zeta(2.0) - std::f64::consts::PI.powi(2) / 6.0).abs() < 1e-12);
        assert!((zeta(2.5) - 1.341_487_257_250_917).abs() < 1e-12);
    }
}
