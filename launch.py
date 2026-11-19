import sys

if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--verify-porting-ui':
        from recheck.porting_ui import verify_saved_view

        raise SystemExit(verify_saved_view(*sys.argv[2:]))
    if '--cli' in sys.argv[1:]:
        from recheck.cli import main

        args = sys.argv[1:]
        args.remove('--cli')
        raise SystemExit(main(args))
    from recheck.gui import main

    raise SystemExit(main())
