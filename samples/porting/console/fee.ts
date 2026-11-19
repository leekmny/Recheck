declare const process: {
    argv: string[];
    env: Record<string, string | undefined>;
    exitCode: number;
};

const units = Number(process.argv[2]);
if (process.argv.length !== 3 || !/^[+-]?\d+$/.test(process.argv[2]) ||
    !Number.isSafeInteger(units) || units < 0 || units > 100) {
    console.error("invalid");
    process.exitCode = 2;
} else {
    const threshold: number = process.env.FEE_DEFECT === "1" ? 4 : 3;
    console.log(Math.min(units, threshold) * 100 + Math.max(0, units - threshold) * 75);
}
