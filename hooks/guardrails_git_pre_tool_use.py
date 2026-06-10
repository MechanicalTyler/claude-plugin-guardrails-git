#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# ///

import json
import shlex
import sys
import re
import subprocess


def create_timeout_error_message(command_type, wanted_timeout, current_timeout):
    minutes = int(wanted_timeout / 60 / 1000)
    return f"You must set a timeout of {wanted_timeout}ms ({minutes} minutes) for {command_type} commands. This is not a test timeout error. Please retry the command with 'timeout': {wanted_timeout} in the tool parameters."


# --- Branch policy: tokenize-based enforcement -------------------------------
#
# Policy: new branches may only be created from 'main', and 'main' itself may
# not be checked out. Enforced structurally (shlex tokens, per shell segment)
# rather than by substring-matching one literal command form, so equivalent
# forms (git branch <name>, git switch -c, git worktree add, chained commands,
# git -C <dir> ...) are all covered.

GIT_GLOBAL_VALUE_OPTS = {'-C', '-c', '--exec-path', '--git-dir', '--work-tree',
                         '--namespace', '--super-prefix', '--config-env',
                         '--list-cmds', '--attr-source'}
WRAPPER_TOKENS = {'env', 'command', 'exec', 'nohup', 'time'}
# Any of these flags means 'git branch' is listing/deleting/moving/copying/
# configuring — not creating a branch.
BRANCH_NON_CREATE_OPTS = {'-d', '-D', '--delete', '-m', '-M', '--move',
                          '-c', '-C', '--copy', '-l', '--list', '-a', '--all',
                          '-r', '--remotes', '--show-current', '-v', '-vv',
                          '--verbose', '--merged', '--no-merged', '--contains',
                          '--no-contains', '--points-at', '--sort', '--format',
                          '--column', '--edit-description', '--set-upstream-to',
                          '-u', '--unset-upstream'}
CHECKOUT_CREATE_OPTS = {'-b', '-B', '--orphan'}
SWITCH_CREATE_OPTS = {'-c', '-C', '--create', '--force-create', '--orphan'}

BRANCH_CREATION_REASON = ("New branches should only be created from 'main', but you're currently on "
                          "'{branch}'. You're already on the correct branch for your work, so no need "
                          "to create a new one.")
CHECKOUT_MAIN_REASON = ("Claude isn't allowed to checkout the main branch. "
                        "Please ask the user what to do instead.")
WORKTREE_REASON = ("Creating git worktrees is not allowed — 'git worktree add' creates branches and "
                   "working trees outside the branch policy. Please continue working on the current "
                   "branch instead.")


def get_current_branch(git_dir=None):
    cmd = ['git'] + (['-C', git_dir] if git_dir else []) + ['branch', '--show-current']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def tokenize_segments(command):
    """Split a shell command into segments of tokens, one per `;`/`&&`/`||`/`|`
    separated simple command. Raises ValueError on unbalanced quoting."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segments, current = [], []
    for tok in lex:
        if tok and all(ch in ';|&()' for ch in tok):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def parse_git_invocation(segment):
    """If the segment invokes git, return (subcommand, args, git_dir from -C);
    otherwise None. Skips leading env assignments, wrappers, and git global options."""
    i = 0
    while i < len(segment) and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', segment[i])
                                or segment[i] in WRAPPER_TOKENS):
        i += 1
    if i >= len(segment):
        return None
    prog = segment[i]
    if prog != 'git' and not prog.endswith('/git'):
        return None
    i += 1
    git_dir = None
    while i < len(segment):
        tok = segment[i]
        if tok in GIT_GLOBAL_VALUE_OPTS:
            if tok == '-C' and i + 1 < len(segment):
                git_dir = segment[i + 1]
            i += 2
        elif tok.startswith('-'):
            i += 1
        else:
            return tok, segment[i + 1:], git_dir
    return None


def _is_short_sticky(tok, opts):
    """True for git's stuck short-option form, e.g. '-bname' for '-b name'."""
    return (tok.startswith('-') and not tok.startswith('--')
            and len(tok) > 2 and tok[:2] in opts)


def _classify_checkout_switch(args, create_opts):
    """Return (creates_branch, first_positional_target) for checkout/switch args."""
    creates, target = False, None
    for tok in args:
        if tok == '--':
            break
        if tok.split('=', 1)[0] in create_opts or _is_short_sticky(tok, create_opts):
            creates = True
        elif not tok.startswith('-') and target is None:
            target = tok
    return creates, target


def _branch_creates(args):
    """True when 'git branch <args>' creates a branch (positional name, no
    listing/delete/move/copy/upstream flag)."""
    positionals = []
    for tok in args:
        if tok == '--':
            break
        key = tok.split('=', 1)[0] if tok.startswith('-') else tok
        if key in BRANCH_NON_CREATE_OPTS or _is_short_sticky(tok, BRANCH_NON_CREATE_OPTS):
            return False
        if not tok.startswith('-'):
            positionals.append(tok)
    return bool(positionals)


def evaluate_git_invocation(subcommand, args, git_dir):
    """Return a block decision dict for a policy violation, else None."""
    if subcommand in ('checkout', 'switch'):
        create_opts = CHECKOUT_CREATE_OPTS if subcommand == 'checkout' else SWITCH_CREATE_OPTS
        creates, target = _classify_checkout_switch(args, create_opts)
        if creates:
            branch = get_current_branch(git_dir)
            if branch and branch != 'main':
                return {"decision": "block", "reason": BRANCH_CREATION_REASON.format(branch=branch)}
        elif target == 'main':
            return {"decision": "block", "reason": CHECKOUT_MAIN_REASON}
    elif subcommand == 'branch':
        if _branch_creates(args):
            branch = get_current_branch(git_dir)
            if branch and branch != 'main':
                return {"decision": "block", "reason": BRANCH_CREATION_REASON.format(branch=branch)}
    elif subcommand == 'worktree':
        if args and args[0] == 'add':
            return {"decision": "block", "reason": WORKTREE_REASON}
    return None


def _branch_policy_fallback(command):
    """Conservative regex check used when the command can't be tokenized
    (e.g. unbalanced quotes), so the guard stays closed."""
    if (re.search(r'\bgit\s+checkout\s+-[bB]\b', command)
            or re.search(r'\bgit\s+switch\s+(?:-[cC]\b|--create\b|--force-create\b)', command)
            or re.search(r'\bgit\s+branch\s+(?!-)[^\s;|&]+', command)):
        branch = get_current_branch()
        if branch and branch != 'main':
            return {"decision": "block", "reason": BRANCH_CREATION_REASON.format(branch=branch)}
    if re.search(r'\bgit\s+(?:checkout|switch)\s+main\b', command):
        return {"decision": "block", "reason": CHECKOUT_MAIN_REASON}
    if re.search(r'\bgit\s+worktree\s+add\b', command):
        return {"decision": "block", "reason": WORKTREE_REASON}
    return None


def check_git_branch_policy(tool_name, tool_input):
    if tool_name != 'Bash':
        return
    command = tool_input.get('command', '')
    try:
        segments = tokenize_segments(command)
    except ValueError:
        decision = _branch_policy_fallback(command)
        if decision:
            print(json.dumps(decision))
            sys.exit(1)
        return
    for segment in segments:
        invocation = parse_git_invocation(segment)
        if invocation:
            decision = evaluate_git_invocation(*invocation)
            if decision:
                print(json.dumps(decision))
                sys.exit(1)


# --- End branch policy --------------------------------------------------------


def check_git_commit_branch(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if re.search(r'\bgit\s+commit\b', command):
            try:
                result = subprocess.run(['git', 'branch', '--show-current'],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    current_branch = result.stdout.strip()
                    if current_branch == 'main':
                        decision = {
                            "decision": "block",
                            "reason": "Direct commits to the 'main' branch are not allowed. Please create a feature branch first (e.g., 'git checkout -b feature/your-feature-name') and commit your changes there, then create a pull request."
                        }
                        print(json.dumps(decision))
                        sys.exit(1)
            except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
                pass


def check_git_no_verify(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if re.search(r'\bgit\s+(?:(?!-m|<<)[^;|&\n])*--no-verify\b', command):
            decision = {
                "decision": "block",
                "reason": "The '--no-verify' flag is not allowed in git commands. You are never allowed to skip hooks"
            }
            print(json.dumps(decision))
            sys.exit(1)


def check_git_commit_boilerplate(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if re.search(r'\bgit\s+commit\b', command):
            boilerplate_patterns = [
                r'Generated with Claude Code',
                r'Co-Authored by Claude',
                r'Generated with \[Claude Code\]',
                r'Co-Authored-By:\s*Claude',
                r'<noreply@anthropic\.com>',
                r'claude\.ai/code',
                r'Generated by Claude',
                r'Created with Claude Code',
                r'Assisted by Claude',
                r'With help from Claude',
                r'@anthropic\.com',
            ]
            for pattern in boilerplate_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    decision = {
                        "decision": "block",
                        "reason": "Boilerplate code patterns are not allowed in git commit messages. Please remove references to Claude Code, co-authorship with Claude, or Anthropic email addresses from your commit message and try again."
                    }
                    print(json.dumps(decision))
                    sys.exit(1)


def check_pr_create_boilerplate(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if re.search(r'\bgh\s+pr\s+create\b', command):
            boilerplate_patterns = [
                r'Generated with Claude Code',
                r'Co-Authored by Claude',
                r'Generated with \[Claude Code\]',
                r'Co-Authored-By:\s*Claude',
                r'<noreply@anthropic\.com>',
                r'claude\.ai/code',
                r'Generated by Claude',
                r'Created with Claude Code',
                r'Assisted by Claude',
                r'With help from Claude',
                r'@anthropic\.com',
                r'AI-generated',
                r'AI generated',
                r'Generated by AI',
                r'Created by AI',
                r'Built with AI',
            ]
            for pattern in boilerplate_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    decision = {
                        "decision": "block",
                        "reason": "Boilerplate patterns are not allowed in PR titles or descriptions. Please remove references to Claude Code, AI generation, co-authorship with Claude, or Anthropic email addresses from your PR content and try again."
                    }
                    print(json.dumps(decision))
                    sys.exit(1)


def check_pr_comment_boilerplate(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        if re.search(r'\bgh\s+api\s+repos/.+/issues/\d+/comments\b', command):
            boilerplate_patterns = [
                r'Generated with Claude Code',
                r'Co-Authored by Claude',
                r'Generated with \[Claude Code\]',
                r'Co-Authored-By:\s*Claude',
                r'<noreply@anthropic\.com>',
                r'claude\.ai/code',
                r'Generated by Claude',
                r'Created with Claude Code',
                r'Assisted by Claude',
                r'With help from Claude',
                r'@anthropic\.com',
                r'AI-generated',
                r'AI generated',
                r'Generated by AI',
                r'Created by AI',
                r'Built with AI',
            ]
            for pattern in boilerplate_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    decision = {
                        "decision": "block",
                        "reason": "Boilerplate patterns are not allowed in PR comments. Please remove references to Claude Code, AI generation, co-authorship with Claude, or Anthropic email addresses from your comment and try again."
                    }
                    print(json.dumps(decision))
                    sys.exit(1)


def add_timeout_to_git_commit(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        timeout = tool_input.get('timeout')
        if re.search(r'\bgit\s+commit\b', command):
            wanted_timeout = 900000
            if timeout != wanted_timeout:
                decision = {
                    "decision": "block",
                    "reason": create_timeout_error_message("Git commit", wanted_timeout, timeout)
                }
                print(json.dumps(decision))
                sys.exit(1)


def add_timeout_to_git_push(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        timeout = tool_input.get('timeout')
        if re.search(r'\bgit\s+push\b', command):
            wanted_timeout = 900000
            if timeout != wanted_timeout:
                decision = {
                    "decision": "block",
                    "reason": create_timeout_error_message("Git push", wanted_timeout, timeout)
                }
                print(json.dumps(decision))
                sys.exit(1)


def add_timeout_to_gh_run_watch(tool_name, tool_input):
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        timeout = tool_input.get('timeout')
        if re.search(r'\bgh\s+run\s+watch\b', command):
            wanted_timeout = 1800000
            if timeout != wanted_timeout:
                decision = {
                    "decision": "block",
                    "reason": create_timeout_error_message("gh run watch", wanted_timeout, timeout)
                }
                print(json.dumps(decision))
                sys.exit(1)


def main():
    try:
        input_data = json.load(sys.stdin)
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})

        add_timeout_to_git_commit(tool_name, tool_input)
        add_timeout_to_git_push(tool_name, tool_input)
        add_timeout_to_gh_run_watch(tool_name, tool_input)
        check_git_commit_branch(tool_name, tool_input)
        check_git_branch_policy(tool_name, tool_input)
        check_git_no_verify(tool_name, tool_input)
        check_git_commit_boilerplate(tool_name, tool_input)
        check_pr_create_boilerplate(tool_name, tool_input)
        check_pr_comment_boilerplate(tool_name, tool_input)

        sys.exit(0)
    except json.JSONDecodeError:
        sys.exit(0)
    except Exception:
        sys.exit(0)


if __name__ == '__main__':
    main()
