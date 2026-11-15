//! Simulated time in integer nanoseconds: the rules of Model Semantics §1,
//! exactly as `smolpy.model.timebase` implements them (ADR-0007, revision of
//! 2026-11-14).
//!
//! - A scenario number means its shortest decimal value, as written in the
//!   scenario JSON: `0.1` ms is exactly 1/10 ms.
//! - A time quantity is computed from such values exactly, then rounded once
//!   to the nearest nanosecond, ties to even.
//! - A periodic source's k-th event is at `start + round(k * period)`, with
//!   the period kept exact.
//! - A metric value is an exact quotient rounded to a float once.

pub const NS_PER_US: u128 = 1_000;
pub const NS_PER_MS: u128 = 1_000_000;
pub const NS_PER_S: u128 = 1_000_000_000;
pub const NS_PER_M: u128 = 5; // propagation at 2 * 10^8 m/s
pub const MAX_NS: u64 = (1 << 63) - 1;

/// A non-negative rational number, kept reduced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Q {
    pub num: u128,
    pub den: u128,
}

fn gcd(mut a: u128, mut b: u128) -> u128 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}

fn overflow() -> String {
    "a time quantity is too large to compute exactly".to_string()
}

impl Q {
    pub fn int(n: u128) -> Q {
        Q { num: n, den: 1 }
    }

    pub fn new(num: u128, den: u128) -> Result<Q, String> {
        if den == 0 {
            return Err("division by zero".to_string());
        }
        let g = gcd(num, den).max(1);
        Ok(Q {
            num: num / g,
            den: den / g,
        })
    }

    /// The exact value of a JSON number as written: `2500`, `0.1`, `1e-07`,
    /// `1e+16`. Only non-negative values occur in the model.
    pub fn parse_decimal(text: &str) -> Result<Q, String> {
        let bad = || format!("not a non-negative decimal number: {text:?}");
        let (mantissa, exponent) = match text.find(['e', 'E']) {
            Some(i) => (&text[..i], text[i + 1..].parse::<i32>().map_err(|_| bad())?),
            None => (text, 0),
        };
        let (int_part, frac_part) = match mantissa.find('.') {
            Some(i) => (&mantissa[..i], &mantissa[i + 1..]),
            None => (mantissa, ""),
        };
        if int_part.is_empty() && frac_part.is_empty() {
            return Err(bad());
        }
        let digits = format!("{int_part}{frac_part}");
        if !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(bad());
        }
        let digits = digits.trim_start_matches('0');
        let digits = if digits.is_empty() { "0" } else { digits };
        let mant: u128 = digits.parse().map_err(|_| overflow())?;
        let exp10 = exponent - frac_part.len() as i32;
        if exp10 >= 0 {
            let scale = 10u128.checked_pow(exp10 as u32).ok_or_else(overflow)?;
            Ok(Q::int(mant.checked_mul(scale).ok_or_else(overflow)?))
        } else {
            let scale = 10u128.checked_pow((-exp10) as u32).ok_or_else(overflow)?;
            Q::new(mant, scale)
        }
    }

    pub fn checked_mul(self, o: Q) -> Result<Q, String> {
        // cross-reduce first so the products stay small
        let g1 = gcd(self.num, o.den).max(1);
        let g2 = gcd(o.num, self.den).max(1);
        let num = (self.num / g1)
            .checked_mul(o.num / g2)
            .ok_or_else(overflow)?;
        let den = (self.den / g2)
            .checked_mul(o.den / g1)
            .ok_or_else(overflow)?;
        Q::new(num, den)
    }

    pub fn checked_div(self, o: Q) -> Result<Q, String> {
        self.checked_mul(Q::new(o.den, o.num)?)
    }

    pub fn checked_add(self, o: Q) -> Result<Q, String> {
        let g = gcd(self.den, o.den).max(1);
        let den = (self.den / g).checked_mul(o.den).ok_or_else(overflow)?;
        let a = self.num.checked_mul(den / self.den).ok_or_else(overflow)?;
        let b = o.num.checked_mul(den / o.den).ok_or_else(overflow)?;
        Q::new(a.checked_add(b).ok_or_else(overflow)?, den)
    }

    pub fn round(self) -> u128 {
        round_ratio(self.num, self.den)
    }
}

/// `round(num / den)`, ties to even.
pub fn round_ratio(num: u128, den: u128) -> u128 {
    let (q, r) = (num / den, num % den);
    let twice = r * 2; // r < den, and den < 2^127 in this model
    if twice > den || (twice == den && q & 1 == 1) {
        q + 1
    } else {
        q
    }
}

fn to_ns(q: Q) -> Result<u64, String> {
    let ns = q.round();
    if ns > MAX_NS as u128 {
        return Err(overflow());
    }
    Ok(ns as u64)
}

/// A duration, delay, cycle or `every` in ms, in nanoseconds.
pub fn ms_to_ns(ms: Q) -> Result<u64, String> {
    to_ns(ms.checked_mul(Q::int(NS_PER_MS))?)
}

/// A period given in ms, as an exact number of nanoseconds.
pub fn period_ns(ms: Q) -> Result<Q, String> {
    ms.checked_mul(Q::int(NS_PER_MS))
}

/// The exact interval of a source with `rate` events per second.
pub fn interval_ns(rate: Q) -> Result<Q, String> {
    Q::int(NS_PER_S).checked_div(rate)
}

/// Propagation delay of a link of `length_m` metres.
pub fn propagation_ns(length_m: Q) -> Result<u64, String> {
    to_ns(length_m.checked_mul(Q::int(NS_PER_M))?)
}

/// Transmission time of `bytes` at `speed_mbps`.
pub fn transmission_ns(bytes: u64, speed_mbps: Q) -> Result<u64, String> {
    to_ns(Q::int(bytes as u128 * 8 * NS_PER_US).checked_div(speed_mbps)?)
}

/// The instants `start + round(k * period)` for k = 1, 2, ..., in integer
/// arithmetic only.
pub struct Instants {
    start: u64,
    num: u128,
    den: u128,
    total: u128,
}

impl Instants {
    pub fn new(start: u64, period: Q) -> Instants {
        Instants {
            start,
            num: period.num,
            den: period.den,
            total: 0,
        }
    }
}

impl Iterator for Instants {
    type Item = u64;
    fn next(&mut self) -> Option<u64> {
        self.total += self.num;
        let offset = if self.den == 1 {
            self.total
        } else {
            round_ratio(self.total, self.den)
        };
        Some(self.start + offset as u64)
    }
}

/// A random draw (a float number of nanoseconds) as a delay: round half to
/// even, as Python's `round()`.
pub fn draw_ns(value_ns: f64) -> u64 {
    value_ns.round_ties_even() as u64
}

/// `n / d` rounded to the nearest f64 once, ties to even, as Python's exact
/// `int / int` (and `float(Fraction)`).
pub fn ratio_f64(n: u128, d: u128) -> f64 {
    assert!(d != 0, "ratio with a zero denominator");
    if n == 0 {
        return 0.0;
    }
    if n < (1 << 53) && d < (1 << 53) {
        return n as f64 / d as f64; // both exact: IEEE division rounds once
    }
    ratio_f64_long(n, d)
}

/// The general case of [`ratio_f64`], by long division to 55 significant
/// bits plus a sticky bit. Requires `d < 2^127`.
pub fn ratio_f64_long(n: u128, d: u128) -> f64 {
    assert!(d < (1 << 127));
    if n == 0 {
        return 0.0;
    }
    let bits = |x: u128| 128 - x.leading_zeros() as i32;
    let mut q = n / d;
    let mut r = n % d;
    let mut exp2: i32 = 0;
    let mut sticky = false;
    if bits(q) > 55 {
        let shift = bits(q) - 55;
        sticky = q & ((1u128 << shift) - 1) != 0 || r != 0;
        q >>= shift;
        exp2 = shift;
        r = 0;
    }
    while bits(q) < 55 {
        r <<= 1;
        q <<= 1;
        if r >= d {
            q |= 1;
            r -= d;
        }
        exp2 -= 1;
    }
    sticky = sticky || r != 0;
    // round 55 bits to 53
    let low = q & 3;
    let mut m = q >> 2;
    exp2 += 2;
    let half = low & 2 != 0;
    let rest = low & 1 != 0 || sticky;
    if half && (rest || m & 1 == 1) {
        m += 1;
        if m == 1 << 53 {
            m >>= 1;
            exp2 += 1;
        }
    }
    // m in [2^52, 2^53): value = m * 2^exp2
    let biased = exp2 + 52 + 1023;
    assert!(
        (1..2047).contains(&biased),
        "ratio out of the normal f64 range"
    );
    f64::from_bits(((biased as u64) << 52) | (m as u64 & ((1 << 52) - 1)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(text: &str) -> Q {
        Q::parse_decimal(text).unwrap()
    }

    #[test]
    fn decimal_values() {
        assert_eq!(d("0.1"), Q { num: 1, den: 10 });
        assert_eq!(
            d("1e-07"),
            Q {
                num: 1,
                den: 10_000_000
            }
        );
        assert_eq!(d("1e+16"), Q::int(10_000_000_000_000_000));
        assert_eq!(d("2500"), Q::int(2500));
        assert_eq!(d("50000.0"), Q::int(50000));
        assert_eq!(d("0.0"), Q::int(0));
        assert!(Q::parse_decimal("-1").is_err());
        assert!(Q::parse_decimal("inf").is_err());
    }

    // The vectors of tests/test_timebase.py.
    #[test]
    fn ms_to_ns_vectors() {
        assert_eq!(ms_to_ns(d("0.1")).unwrap(), 100_000);
        assert_eq!(ms_to_ns(d("1.5")).unwrap(), 1_500_000);
        assert_eq!(ms_to_ns(d("10000")).unwrap(), 10_000_000_000);
        assert_eq!(ms_to_ns(d("0.123456789")).unwrap(), 123_457);
    }

    #[test]
    fn ties_go_to_even() {
        let ms: Vec<u64> = ["0.0000005", "0.0000015", "0.0000025", "0.0000035"]
            .iter()
            .map(|t| ms_to_ns(d(t)).unwrap())
            .collect();
        assert_eq!(ms, [0, 2, 2, 4]);
        let prop: Vec<u64> = ["0.1", "0.3", "0.5", "0.7"]
            .iter()
            .map(|t| propagation_ns(d(t)).unwrap())
            .collect();
        assert_eq!(prop, [0, 2, 2, 4]);
        assert_eq!([0.5, 1.5, 2.5, 2.5000001].map(draw_ns), [0, 2, 2, 3]);
    }

    #[test]
    fn transmission() {
        for speed in ["10", "100", "1000", "10.0", "1000.0"] {
            for size in 1..=2000u64 {
                let exact = Q::int(size as u128 * 8000).checked_div(d(speed)).unwrap();
                assert_eq!(exact.den, 1);
                assert_eq!(transmission_ns(size, d(speed)).unwrap() as u128, exact.num);
            }
        }
        assert_eq!(transmission_ns(1500, d("10000")).unwrap(), 1200);
        assert_eq!(transmission_ns(64, d("10000")).unwrap(), 51);
        assert_eq!(transmission_ns(66, d("10000")).unwrap(), 53);
        assert_eq!(transmission_ns(100, d("2.5")).unwrap(), 320_000);
    }

    #[test]
    fn instants_do_not_drift() {
        let period = interval_ns(d("3")).unwrap();
        assert_eq!(
            period,
            Q {
                num: 1_000_000_000,
                den: 3
            }
        );
        let first: Vec<u64> = Instants::new(0, period).take(3).collect();
        assert_eq!(first, [333_333_333, 666_666_667, 1_000_000_000]);
        let k = 1_000_000u64;
        let last = Instants::new(7, period).nth(k as usize - 1).unwrap();
        assert_eq!(last as u128, 7 + round_ratio(k as u128 * 1_000_000_000, 3));
        let every = period_ns(d("0.1")).unwrap();
        assert_eq!(every, Q::int(100_000));
    }

    #[test]
    fn ratio_is_rounded_once() {
        assert_eq!(ratio_f64(1, 3), 1.0 / 3.0);
        assert_eq!(ratio_f64(10u128.pow(30) + 1, 10u128.pow(30)), 1.0);
        assert_eq!(ratio_f64(1_500_000, 1_000_000), 1.5);
        assert_eq!(ratio_f64(96_301, 3_000), 96_301.0 / 3_000.0);
        // the long division agrees with IEEE division wherever that is exact
        let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
        for _ in 0..20_000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let n = (x >> 11) as u128 + 1;
            let dd = ((x.rotate_left(29) >> 11) as u128).max(1);
            assert_eq!(ratio_f64_long(n, dd), n as f64 / dd as f64, "{n} / {dd}");
        }
    }
}
