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


# --- Branch policy ------------------------------------------------------------
#
# Policy: new branches may only be created from 'main', and 'main' itself may
# not be checked out. Enforced on shlex tokens per shell segment so equivalent
# forms (git branch <name>, git switch -c, git worktree add, chained commands,
# git -C <dir> ...) are all covered, not just 'git checkout -b'.

# Any of these flags means 'git branch' is listing/deleting/moving/copying/
# configuring — not creating a branch.
BRANCH_NON_CREATE_OPTS = {'-d', '-D', '--delete', '-m', '-M', '--move',
                          '-c', '-C', '--copy', '-l', '--list', '-a', '--all',
                          '-r', '--remotes', '--show-current', '-v', '-vv',
                          '--verbose', '--merged', '--no-merged', '--contains',
                          '--no-contains', '--points-at', '--sort', '--format',
                          '--column', '--edit-description', '--set-upstream-to',
                          '-u', '--unset-upstream'}
# Branch-creating flags of 'git checkout' (-b/-B) and 'git switch' (-c/-C, --create).
CREATE_OPTS = {'-b', '-B', '-c', '-C', '--create', '--force-create', '--orphan'}

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
    """Split a shell command into token lists, one per `;`/`&&`/`||`/`|`
    separated simple command. Raises ValueError on unbalanced quoting."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segments, current = [], []
    for tok in lex:
        if all(ch in ';|&()' for ch in tok):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def parse_git_invocation(segment):
    """If the segment invokes git, return (subcommand, args, dir from -C);
    otherwise None. Skips git's global options to find the subcommand."""
    if 'git' not in segment:
        return None
    rest = segment[segment.index('git') + 1:]
    git_dir = None
    while rest and rest[0].startswith('-'):
        opt = rest.pop(0)
        if opt == '-C' and rest:
            git_dir = rest.pop(0)
        elif opt in ('-c', '--git-dir', '--work-tree') and rest:
            rest.pop(0)
    if not rest:
        return None
    return rest[0], rest[1:], git_dir


def _matches_opt(tok, opts):
    """True if tok is one of opts, in --opt=value form, or in git's stuck
    short-option form ('-bname' for '-b name')."""
    return (tok.split('=', 1)[0] in opts
            or (not tok.startswith('--') and len(tok) > 2 and tok[:2] in opts))


def evaluate_git_invocation(subcommand, args, git_dir):
    """Return a block reason for a policy violation, else None."""
    creates = False
    if subcommand in ('checkout', 'switch'):
        flags = [tok for tok in args if tok.startswith('-') and tok != '--']
        positionals = [tok for tok in args if not tok.startswith('-')]
        creates = any(_matches_opt(tok, CREATE_OPTS) for tok in flags)
        if not creates and positionals and positionals[0] == 'main':
            return CHECKOUT_MAIN_REASON
    elif subcommand == 'branch':
        flags = [tok for tok in args if tok.startswith('-') and tok != '--']
        positionals = [tok for tok in args if not tok.startswith('-')]
        creates = bool(positionals) and not any(
            _matches_opt(tok, BRANCH_NON_CREATE_OPTS) for tok in flags)
    elif subcommand == 'worktree':
        if args[:1] == ['add']:
            return WORKTREE_REASON
    if creates:
        branch = get_current_branch(git_dir)
        if branch and branch != 'main':
            return BRANCH_CREATION_REASON.format(branch=branch)
    return None


def check_git_branch_policy(tool_name, tool_input):
    if tool_name != 'Bash':
        return
    try:
        segments = tokenize_segments(tool_input.get('command', ''))
    except ValueError:
        return  # unbalanced quoting — the shell will reject the command anyway
    for segment in segments:
        invocation = parse_git_invocation(segment)
        reason = evaluate_git_invocation(*invocation) if invocation else None
        if reason:
            print(json.dumps({"decision": "block", "reason": reason}))
            sys.exit(1)


# --- End branch policy ----------------------------------------------------------


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
