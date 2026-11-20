# Recheck

[한국어](README.md) | **English**

**0.2.1 source · Windows x64 preview release**

## Project purpose

Recheck compares programs and distributions you provide and records reproducible evidence of differences in observed behavior, structure, and contents. It records what changed with the same input or scenario, whether each target met the expected result, and what the checks covered. You can save inputs that produced differences and use them to recheck a revised version.

Cross-language porting validation is one use case. Recheck also supports regression checks between versions of the same program, behavior checks for a new implementation or replacement candidate, function structure reviews, and comparisons of distribution package contents. The capstone focused on validating a replacement for a legacy program.

The public repository includes source code, tests, self-authored samples, and documentation on the design and verification scope. See [LICENSE](LICENSE) for rights to the original code and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the terms of external components. Access to a public repository does not imply permission to reuse it as open source. The [capstone requirements, evidence, and presentation guide](https://github.com/leekmny/Recheck/blob/v0.1.7/docs/CAPSTONE_FEEDBACK.md) covers the roles of the original program, reimplementation, external tools, and Recheck, along with examples of conflicting metrics, the scope of the experiments, and open research questions.

Function structure scores are a heuristic for finding candidates to compare. They do not measure behavioral agreement or the likelihood of copyright infringement. The comparison feature does not aim to automate copying, decryption, or determinations about rights. The GUI's Reconstruct and Modernize actions and the CLI's `reconstruct` and `modernize` commands currently record requests and preserve the original inputs. The pipeline for source generation, builds, and differential validation is disabled because it has yet to be implemented.

The analysis AI setup guide lists the recommended ChatGPT option first, then the free Gemini API, then connections to other AI accounts. Sign-in and API key issuance open in an external browser by default. A checkbox lets you choose the embedded view. See [Google's restrictions and the rationale for the default](https://github.com/leekmny/Recheck/blob/main/docs/decisions/0006-external-browser-default.md).

**[0.2.1 Windows x64 preview](https://github.com/leekmny/Recheck/releases/tag/v0.2.1)** · [All releases](https://github.com/leekmny/Recheck/releases) · [Previous 0.1.6 release](https://github.com/leekmny/Recheck/releases/tag/v0.1.6)

Version 0.1.7 excludes structure scores for unobserved functions and shows how many execution results can be compared, why results were excluded, and how the raw outputs differ. If the output policy for an earlier record cannot be established, Recheck reports the raw observation count separately from the count it can evaluate.

Version 0.1.6 brought together setup guides for ChatGPT sign-in and Gemini, OpenAI, Grok, DeepSeek, and Claude API keys. It also streamlined checking or disconnecting AI connections and choosing one for each comparison. Set up a connection in **Settings → Analysis AI connections** (설정 → 분석 AI 연결), then choose one AI for the current analysis in **Program comparison** (프로그램 비교). Recheck does not automatically send the same analysis to multiple AI providers. The earlier 0.1.5 EXE does not include the integrated screen. See the corresponding release and [work log](https://github.com/leekmny/Recheck/blob/main/docs/decisions/2026-11-17-embedded-login-work-log.md) for final execution checks and publication results for the new ZIP. See also the [connection instructions and limits](docs/AI_CONNECTIONS.md) and [verification status for real accounts](docs/AI_AUTH_STATUS.md).

The `0.1.x` releases are early development versions. The [previous version names](docs/PUBLIC_HISTORY.md#공개-버전-이름-정리) table records changes to the public versions. Each release distinguishes the scope of the source checks from the execution checks for the Windows distribution.

Extract the ZIP and run **`Recheck.exe`** in the `Recheck` folder. The distribution bundles Python and Qt, so you can launch the app without installing Git or Python. GitHub's `Source code` ZIP contains the development source. See the [first-run guide](docs/FIRST_RUN.md) and [binary distribution materials](docs/BINARY_DISTRIBUTION.md). Earlier prototypes and original development records are preserved in a private archive repository.

This public repository starts a new Git history from a reviewed source snapshot. Earlier commits, pull requests, Actions history, and tags are outside that public history. [Public history](docs/PUBLIC_HISTORY.md) explains what was preserved and the workflow for subsequent changes.

The PySide6 GUI and Python CLI share a comparison engine. You can inspect the evidence and status separately for static observations from Ghidra and REA, runtime observations, and explanations from the selected AI.

Choose **Static analysis**, **Dynamic analysis**, or **Both** (정적분석·동적분석·둘 다 분석). The default is **Both**. AI explanations have a separate toggle. If a tool required by the selected mode is unavailable, the app shows how to install or prepare it before analysis and keeps the selected mode. Standard binary dynamic analysis supports Linux x64 ELF console programs. Windows and GUI targets use target specifications and the scenario comparison workflow for the corresponding environment. The .NET comparison covers method signatures, CIL bytes, and exception metadata. The app does not present this as C# source reconstruction or runtime validation. See the [analysis mode guide](docs/ANALYSIS_MODES.md) and [support matrix](docs/DISTRIBUTION_AND_RECOVERY.md#파일-지원-매트릭스).

## Porting scenario validation

In **Program comparison → Porting scenario comparison** (프로그램 비교 → 포팅 시나리오 비교), choose specifications and isolated environments for runtime, API, GUI, and library targets. Use the same scenario to compare implementations in different languages or two versions of the same program. You can inspect the observed behavior match rate, whether each target met its expected results, and the structural features separately. [Setup, reproduction, and measurement scope](docs/PORTING_VALIDATION_UPDATE.md)

Version 0.2.1 uses the same copies of the inputs, prepared at the start of the comparison, for scenario and structural analysis. Recheck writes observations to SQLite at each stage, so you can reopen saved observations after an interruption. Earlier results remain accessible.

## Current features and scope

- Select program files, write or import test inputs, run the targets in isolation, and compare the results.
- Check tools and get installation guidance by analysis mode. Results and reports track static analysis, dynamic analysis, and AI status separately.
- Review the actual collection scope, unobserved functions, and valid raw execution observations in the coverage view.
- Save mismatched or unmeasured inputs, recheck them in a new record, and export only failed items as HTML or JSON.
- Compare files added, removed, or changed in ZIP archives and ZIP-based packages, and export the results as JSON or HTML.
- Inspect function and execution evidence, get AI explanations and additional analysis, and review reports and past analyses.
- Get help connecting through the recommended ChatGPT sign-in, the free Gemini API, or other AI providers.
- Find tool installations outside PATH, install required tools through WinGet, and find official downloads.
- Configure connections to Ollama, the OpenAI API, the Claude API, Gemini, and OpenAI-compatible servers.
- Retrieve the distribution's analysis environment automatically. Static analysis with Java, Ghidra, and REA runs in a separate container from target execution.
- Set up local AI in one click. Prepare a CPU-only Ollama runtime, download Qwen3 4B Instruct, check for a response, and select the model.

Recheck supports execution comparisons for Linux x86-64 ELF console programs. Execution comparisons for Windows programs require a separate Windows worker. A standard Windows container cannot stand in for desktop GUI installation testing. A full Windows VM can run in QEMU inside Linux Docker. The KVM API and creation of an empty VM were verified on the test PC. Windows boot, PE execution, and installation in a clean VM remain unverified. See the [Windows Docker investigation and setup procedure](docs/WINDOWS_DOCKER.md). Full folder comparison and Rebuild are outside the current scope. Matching results for registered inputs do not establish equivalence for all inputs. See the [support, distribution, and recovery guide](docs/DISTRIBUTION_AND_RECOVERY.md) and the [container environment guide](docs/CONTAINER_RUNTIME.md) for scope and limitations.

## Using the app

Extract the Windows distribution ZIP and **run `Recheck.exe` with the entire Recheck folder intact**. The `_internal` folder contains Python, Qt, and app resources. The app sets up Docker images for the managed analysis environment over the internet the first time you prepare it. Execution comparisons require Docker Desktop and WSL 2 with support for Linux containers. An internet connection is also required for external AI providers such as Gemini and GPT. **With AI explanations disabled, you can run analyses without an API key or AI connection.**

1. In **Program comparison** (프로그램 비교), select static analysis, dynamic analysis, or both, then choose the existing program and the new program. You can check the required tools and access installation guidance on the same screen.
2. Open **Tool installation and management → Check tools** (도구 설치·관리 → 도구 확인) to prepare the tools required by your selection. To use the managed environment for static or dynamic analysis, start Docker Desktop, then click **Download and prepare analysis tools** (분석 도구 내려받아 준비). Initial setup requires internet access and several GB of disk space. The environment includes Java, Ghidra, and REA. Docker is optional when using your own Ghidra and Java installation for supported static analysis formats, or when comparing ZIP or package contents. See the [analysis mode guide](docs/ANALYSIS_MODES.md) for support by analysis path, including .NET. Installation and image preparation start only when you click the corresponding button.
3. **Connect an AI only if you want AI explanations.** Use the recommended ChatGPT sign-in or **Start with Gemini for free** (Gemini 무료로 시작하기) with your own account or key, then check that the provider returns a response. The free Gemini setup defaults to Gemini 3.5 Flash-Lite, which becomes available after you connect. In **Program comparison**, select one prepared AI connection and one model. Recheck preserves existing connections, keys, and budgets and keeps the current selection fixed during the run. See the [AI connection guide](docs/AI_CONNECTIONS.md) and [free Gemini setup guide](docs/GEMINI_FREE_SETUP.md).
4. If the selected mode includes dynamic analysis, register the inputs to compare and click **Start comparison** (비교 시작하기). The app hides execution inputs in static-only mode. If the tools or files do not meet the support requirements, follow the guidance to prepare them or change the analysis mode manually.
5. Review the actual outputs, exit codes, and mismatched inputs first, then inspect the AI explanations and reports.

In the results, open **Coverage** (확인 범위) to inspect observations and gaps for functions, pseudocode, relationships, and each input. Under **Save problem input → Saved inputs for rechecking** (문제 입력 저장 → 재검사 보관함), select the original and revised targets to create a new dynamic analysis with the saved inputs and comparison policy. The **Failed items report** (실패 항목 보고서) distinguishes mismatches, unmeasured executions, incomplete static collection, and AI failures, and exports their causes and next steps to HTML or JSON. You can opt in to include raw inputs and outputs. Existing results and personal settings are preserved. See the [coverage, rechecking, and failed items report guide](docs/REVIEW_AND_RECHECK.md).

To set up local AI for the first time, choose **Start with AI on my PC** (내 PC의 AI로 시작). Recheck starts Ollama and downloads the Qwen3 4B Instruct model (about 2.5 GB). If you have already selected the local AI, the app can reconnect to the existing environment at startup. The model is stored in a managed volume outside the distribution ZIP. Responses may be slow because the default local environment uses only the CPU. If a model fails a real tool-call check, Recheck uses it only for basic comparisons and explanations. When you use GPT, Recheck does not start Ollama or download the model.

AI authentication is configured separately in Recheck on each PC. Signing in to GitHub or cloning the repository does not transfer AI connections. See the [AI connection guide](docs/AI_CONNECTIONS.md) and [authentication implementation and verification status](docs/AI_AUTH_STATUS.md) for setup instructions and what has been verified for each provider.

To compare only ZIP contents, choose **Compare ZIP and package files** (ZIP·패키지 구성 파일 비교) in **Program comparison**. This mode compares file paths, types, sizes, and content hashes without Docker, AI, or execution inputs. It can also inspect APK, JAR, MSIX, and IPA files if they are valid ZIP containers. Its scope is limited to package contents and excludes app execution, code semantics, and signatures. See the [package comparison guide](docs/PACKAGE_COMPARISON.md) for instructions and size and format limits.

## Running and modifying the source

After extracting the source ZIP on Windows, run **`Run-Recheck.cmd`**. It looks for an existing Python installation, including outside PATH, offers installation guidance if needed, and prepares a project-specific environment. Running from the source ZIP does not require Git. For development, use Python 3.11 or later and install the pinned dependencies from `requirements-lock.txt`. To run manually:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
.venv\Scripts\python launch.py
```

Run the automated checks after making changes. Check GUI behavior and real external tools and AI connections separately as needed.

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m recheck environment
```

Key repository directories:

| Path | Purpose |
| --- | --- |
| `recheck/` | GUI, CLI, and comparison engine |
| `ai_providers/` | AI provider connections |
| `resources/` | App resources, Ghidra scripts, and Dockerfiles |
| `tests/` | Automated regression checks |
| `scripts/` | Build and verification tasks |
| `docs/` | User and developer guides |

## Building the Windows distribution

The public Windows ZIP includes the runnable app, notices, and corresponding source materials. Users set up Docker images for analysis in the app on first use. The following development commands are for release maintainers who need to build a separate offline environment. Offline images require their own redistribution review.

```powershell
.venv\Scripts\python scripts\build_managed_images.py --help
.venv\Scripts\python scripts\export_managed_environment.py --include-ollama --update-manifest --output-dir environment
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

To build a public app package, run `scripts/verify_source.py --output <new verification directory>` followed by `scripts/package_verified.py --output <new package directory> --app-only`, using new output folders for verification and packaging. `RECHECK_SOURCE_IDENTITY.json` is updated only after the full pytest suite and Node normalization checks pass and the source is identical before and after verification. PyInstaller builds in a new staging folder and leaves the current runnable folder in place. Check the resulting EXE and ZIP separately.

The official output is `dist/Recheck`. After checking the actual EXE, use `release_distribution.py` to replace both the official output and the external user folder. The release procedure preserves the entire previous folder, checks all file hashes including the ZIP, and uses a journal to recover from interruptions. Store the ZIP outside the runnable folder. See the [distribution and recovery guide](docs/DISTRIBUTION_AND_RECOVERY.md) for detailed commands and limitations. Cloning, committing, or pushing by itself does not update the EXE.

## Developing on GitHub

The source and selected documents are publicly viewable. Write access is limited to administrators and invited collaborators. Changes go through branches, pull requests, and checks. Actual analysis inputs, credentials, personal QA materials, VMs, and models stay on each PC. Follow the [security guide](https://github.com/leekmny/Recheck/blob/main/SECURITY.md) when reporting issues. Executables are provided in verified, versioned release ZIPs.

See the [GitHub workflow guide](docs/GITHUB_WORKFLOW.md) for collaboration, verification, and release procedures. [Project status](docs/PROJECT_STATUS.md) records where work paused, the implementation scope of individual REA features, and remaining work.

Team members should start with the [contribution and development history guide](https://github.com/leekmny/Recheck/blob/main/CONTRIBUTING.md) and prepare a working branch and pull request. The [architecture decision records (ADRs)](https://github.com/leekmny/Recheck/blob/main/docs/decisions/README.md) capture important decisions and the alternatives that were adopted or deferred. The [early embedded sign-in experiment](https://github.com/leekmny/Recheck/blob/main/experiments/embedded_login/README.md) documents observations before integration. The current integration follows the [connection design](https://github.com/leekmny/Recheck/blob/main/docs/superpowers/specs/2026-11-17-embedded-provider-connections-design.md).

Download runnable ZIPs from **Releases**. **Packages** is a separate registry for Docker images and development dependencies and is currently unused. The `No packages published` message refers to that registry.
