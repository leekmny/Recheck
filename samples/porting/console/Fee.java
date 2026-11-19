public final class Fee {
    public static void main(String[] args) {
        int units;
        try {
            if (args.length != 1) throw new NumberFormatException();
            units = Integer.parseInt(args[0]);
            if (units < 0 || units > 100) throw new NumberFormatException();
        } catch (NumberFormatException invalid) {
            System.err.println("invalid");
            System.exit(2);
            return;
        }
        int total = 0;
        for (int index = 0; index < units; index++) total += index < 3 ? 100 : 75;
        System.out.println(total);
    }
}
