const fn fee_table() -> [u16; 101] {
    let mut fees = [100; 101];
    let mut weight = 1;
    while weight < fees.len() {
        fees[weight] = fees[weight - 1] + 15;
        weight += 1;
    }
    fees
}

const FEES: [u16; 101] = fee_table();

/// The caller provides at least `len` writable bytes when `out` is non-null.
#[no_mangle]
pub unsafe extern "C" fn parcel_quote(weight: i32, out: *mut u8, len: u32) -> i32 {
    let Some(amount) = FEES.get(weight as usize) else {
        return -1;
    };
    if len < 2 || out.is_null() {
        return -2;
    }
    let adjustment = u16::from(cfg!(defect) && weight == 10);
    let bytes = (amount - adjustment).to_le_bytes();
    unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len()) };
    0
}
