using System;
using System.Globalization;

if (args.Length != 1 ||
    !int.TryParse(args[0], NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var units) ||
    units < 0 || units > 100) {
    Console.Error.WriteLine("invalid");
    return 2;
}
int threshold = Environment.GetEnvironmentVariable("FEE_DEFECT") == "1" ? 4 : 3;
Console.WriteLine(Math.Min(units, threshold) * 100 + Math.Max(0, units - threshold) * 75);
return 0;
