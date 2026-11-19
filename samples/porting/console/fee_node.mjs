const argument = process.argv[2];
const units = Number(argument);
if (process.argv.length !== 3 || !/^[+-]?\d+$/.test(argument) ||
    !Number.isSafeInteger(units) || units < 0 || units > 100) {
  console.error("invalid");
  process.exitCode = 2;
} else {
  const threshold = process.env.FEE_DEFECT === "1" ? 4 : 3;
  console.log(Math.min(units, threshold) * 100 + Math.max(units - threshold, 0) * 75);
}
