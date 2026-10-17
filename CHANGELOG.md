# Changelog

## [16.0.0](https://github.com/Avunu/mercury_integration/compare/v0.0.1...v16.0.0) (2026-10-17)


### Features

* auto-reconcile transfers ([9f9b0c9](https://github.com/Avunu/mercury_integration/commit/9f9b0c90b058ca7a6b47247232b64c46c09f3b3d))
* automatic-reconciliation script ([2c6988b](https://github.com/Avunu/mercury_integration/commit/2c6988ba2a4e1ae0c58281eee145c8ad4db6b922))
* book GL codes against any non-Asset account, party and all ([4e27d0c](https://github.com/Avunu/mercury_integration/commit/4e27d0cac380aaa36a46d40681232a6cad3907a5))
* daily backfill transtranctions ([05be920](https://github.com/Avunu/mercury_integration/commit/05be92021aabea8070397e7d1ab3645ea8671720))
* enhance bank account handling and syncing logic ([72fd6bd](https://github.com/Avunu/mercury_integration/commit/72fd6bd5498949934ddf0e117a6c6eeeea45bbb7))
* export every non-Asset account as a GL code ([9e71147](https://github.com/Avunu/mercury_integration/commit/9e711472463a67ff58c06226e610778618a51ba3))
* Initialize App ([9ecfa21](https://github.com/Avunu/mercury_integration/commit/9ecfa2184adee191b189b4a48547d893e682f853))
* manual recipient link ([a594227](https://github.com/Avunu/mercury_integration/commit/a5942271d04cfda67ed914b47fc217a9553b73a8))
* map GL accounts from Mercury GL Codes, not Categories ([e9bfd6b](https://github.com/Avunu/mercury_integration/commit/e9bfd6b213d2873b8d11db95ab0d538f649a787a))
* match contacts with existing recipients ([8675854](https://github.com/Avunu/mercury_integration/commit/86758541dd0000e22798b3b3a2390dabac8b7791))
* open Mercury's GL code mapping page after exporting the CSV ([487bf5c](https://github.com/Avunu/mercury_integration/commit/487bf5ca13b0805757887ee35fbef83b35c46e47))


### Bug Fixes

* attach Mercury files via File doc instead of legacy save_file ([83ca88b](https://github.com/Avunu/mercury_integration/commit/83ca88b9e7a0bdabd2f2cbe4b9518ba8e306a70c))
* don't round payroll payments ([3c92135](https://github.com/Avunu/mercury_integration/commit/3c921350ccae0ba13f28b692d2d1f6dc4171530c))
* keep slip_payout_amount type-clean for a missing net_pay ([dbf5d10](https://github.com/Avunu/mercury_integration/commit/dbf5d10d2c0c2e4a69e01277c36463432df5d4dd))
* never post a group cost center on an auto journal entry ([8d2e213](https://github.com/Avunu/mercury_integration/commit/8d2e21320939aee08509556b0bfa7c3b1e676cda))
* update webhook status handling in health check ([b75b0e1](https://github.com/Avunu/mercury_integration/commit/b75b0e12fcffdf3dc8000d9e81544412d548cd02))


### Continuous Integration

* adopt the shared CI gates ([4eef9de](https://github.com/Avunu/mercury_integration/commit/4eef9de1b93e5fc7402aaf72fe652af2cb91998f))
