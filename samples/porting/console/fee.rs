fn main() {
    let mut args = std::env::args();
    args.next();
    let units = args.next().and_then(|value| value.parse::<i32>().ok());
    if args.next().is_some() || !matches!(units, Some(0..=100)) {
        eprintln!("invalid");
        std::process::exit(2);
    }
    let units = units.unwrap();
    #[cfg(alternate)]
    let total = units.min(3) * 100 + (units - 3).max(0) * 75;
    #[cfg(not(alternate))]
    let total: i32 = (0..units)
        .map(|i| if i < if cfg!(defect) { 4 } else { 3 } { 100 } else { 75 })
        .sum();
    println!("{total}");
}
