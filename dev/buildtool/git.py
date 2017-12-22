#!/usr/bin/python
#
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper class for issuing git commands."""

# pylint: disable=logging-format-interpolation

import collections
import logging
import os
import re
import shutil
import tempfile
import time

from subprocess import CalledProcessError

# pylint: disable=no-name-in-module
# pylint: disable=import-error
from distutils.version import LooseVersion
import yaml

from buildtool.util import (
    check_subprocess,
    check_subprocess_sequence,
    ensure_dir_exists,
    run_subprocess)


class RemoteGitRepository(
    collections.namedtuple(
        'RemoteGitRepository', ['name', 'url', 'upstream_ref'])):
  """A reference to a remote git repository and possibly it's upstream as well.

  Attributes:
    name: [string] The shorthand name of the repository
    url: [string] The url to the repository isnt necessarily a URL, rather
       this is simply the repository string passed to git clone, which is
       typically a URL or directory name.
    upstream_ref: [RemoteGitRepository] A reference to the upstream repository
       that the url repository is derived from. This is for purposes of
       informing update requests between the origin URL from its upstream URL.
  """

  @staticmethod
  def make_from_url(url, upstream_ref=None):
    """Create a new RemoteGitRepository from the specified URL.

    The tail of the URL is assumed to be the repository name.
    """
    name = url[url.rfind('/') + 1:]
    return RemoteGitRepository(name, url, upstream_ref)

  @staticmethod
  def merge_to_dict(*pos_args):
    """For a upstream_repos parameter value from a list of mappings.

    pos_args: [list of dict] where key is name of repository,
        value is the RemoteGitRepository specification for that repository name.
    """
    result = {}
    for some in pos_args:
      result.update(some)
    return result

  @property
  def upstream_url(self):
    """The upstream repository URL or None if there is no upstream_ref."""
    return None if self.upstream_ref is None else self.upstream_ref.url


class SemanticVersion(
    collections.namedtuple('SemanticVersion',
                           ['series_name', 'major', 'minor', 'patch'])):
  """Helper class for interacting with semantic version tags."""

  SEMVER_MATCHER = re.compile(r'(.+)-(\d+)\.(\d+)\.(\d+)')
  TAG_INDEX = 0
  MAJOR_INDEX = 1
  MINOR_INDEX = 2
  PATCH_INDEX = 3

  @staticmethod
  def make(tag):
    """Create a new SemanticVersion from the given tag instance.

    Args:
      tag: [string] in the form <series_name>-<major>.<minor>.<patch>
    """
    match = SemanticVersion.SEMVER_MATCHER.match(tag)
    if match is None:
      raise ValueError('Malformed tag "{tag}"'.format(tag=tag))

    # Keep first group as a string, but use integers for the component parts
    return SemanticVersion(match.group(1),
                           *[int(num) for num in match.groups()[1:]])

  def most_significant_diff_index(self, arg):
    """Returns the *_INDEX for the most sigificant component differnce."""
    if arg.series_name != self.series_name:
      return self.TAG_INDEX
    if arg.major != self.major:
      return self.MAJOR_INDEX
    if arg.minor != self.minor:
      return self.MINOR_INDEX
    if arg.patch != self.patch:
      return self.PATCH_INDEX

    return None

  def to_version(self):
    """Return string encoding of underlying version number."""
    return '{major}.{minor}.{patch}'.format(
        major=self.major, minor=self.minor, patch=self.patch)

  def to_tag(self):
    """Return string encoding of SemanticVersion tag."""
    return '{series}-{major}.{minor}.{patch}'.format(
        series=self.series_name,
        major=self.major, minor=self.minor, patch=self.patch)

  def next(self, at_index):
    """Returns the next SemanticVersion from this when bumping up.

    Args:
       at_index: [int] The component *_INDEX to bump at.
    """
    if at_index is None:
      raise ValueError('Invalid index={0}'.format(at_index))

    major = self.major
    minor = self.minor
    patch = self.patch

    if at_index == self.PATCH_INDEX:
      patch += 1
    else:
      patch = 0
      if at_index == self.MINOR_INDEX:
        minor += 1
      elif at_index == self.MAJOR_INDEX:
        minor = 0
        major += 1
      else:
        raise ValueError('Invalid index={0}'.format(at_index))

    return SemanticVersion(self.series_name, major, minor, patch)


class CommitTag(
    collections.namedtuple('CommitTag', ['commit_id', 'tag', 'version'])):
  """Denotes an individual result of git show-ref --tags."""

  @staticmethod
  def make(ref_line):
    """Create a new instance from a response line.

    Args:
      ref_line: [string] Response from "git show-ref --tags"
    """
    # ref_line is in the form "$commit_id refs/tags/$tag"
    tokens = ref_line.split(' ')
    line_id = tokens[0]
    tag_parts = tokens[1].split('/')
    tag = tag_parts[len(tag_parts) - 1]
    version = LooseVersion(tag)
    return CommitTag(line_id, tag, version)

  @staticmethod
  def compare_tags(first, second):
    """Comparator for instances compares the lexical order of the tags."""
    return cmp(first.tag, second.tag)

  # pylint: disable=multiple-statements
  def __lt__(self, other): return self.tag.__lt__(other.tag)
  def __le__(self, other): return self.tag.__le__(other.tag)
  def __eq__(self, other): return self.tag.__eq__(other.tag)
  def __ge__(self, other): return self.tag.__ge__(other.tag)
  def __gt__(self, other): return self.tag.__gt__(other.tag)
  def __ne__(self, other): return self.tag.__ne__(other.tag)


class CommitMessage(
    collections.namedtuple('CommitMessage',
                           ['commit_id', 'author', 'date', 'message'])):
  """Denotes an individual entry in 'git log --pretty'."""
  _MEDIUM_PRETTY_COMMIT_MATCHER = re.compile(
      '(.+)\nAuthor: *(.+)\nDate: *(.*)\n', re.MULTILINE)

  _EMBEDDED_COMMIT_MATCHER = re.compile(
      r'^( *)commit [a-f0-9]+\n'
      r'^\s*Author: .+\n'
      r'^\s*Date:   .+\n',
      re.MULTILINE)

  _EMBEDDED_SUMMARY_MATCHER = re.compile(
      r'^\s*(?:\*\s*)?[a-z]+\(.+?\): .+',
      re.MULTILINE)

  # The vocabulary in the following list was taken looking at what's
  # used in practice (right or wrong), for conforming log entries,
  # using the following command over the various spinnaker repositories:
  #
  # git log --pretty=oneline \
  #   | sed "s/^[a-f0-9]\+ //g" \
  #   | egrep "^[^ \(]+\(" \
  #   | sed "s/^\([^\(]\+\).*/\1/g" \
  #   | sort | uniq
  DEFAULT_PATCH_REGEXS = [
      # Some tags indicate only a patch release.
      re.compile(r'^\s*'
                 r'(?:\*\s+)?'
                 r'((?:fix|bug|docs?|test)[\(:].*)',
                 re.MULTILINE)
  ]
  DEFAULT_MINOR_REGEXS = [
      # Some tags indicate a minor release.
      # These are features as well as hints to non-trivial
      # implementation changes that suggest a higher level of risk.
      re.compile(r'^\s*'
                 r'(?:\*\s+)?'
                 r'((?:feat|feature|chore|refactor|perf|config)[\(:].*)',
                 re.MULTILINE)
  ]
  DEFAULT_MAJOR_REGEXS = [
      # Breaking changes are explicitly marked as such.
      re.compile(r'^\s*'
                 r'(.*?BREAKING CHANGE.*)',
                 re.MULTILINE)
  ]

  @staticmethod
  def make_list_from_result(response_text):
    """Returns a list of CommitMessage from the command response.

    Args:
      response_text: [string] result of "git log --pretty=medium"
    """
    all_entries = ('\n' + response_text.strip()).split('\ncommit ')[1:]
    response = []
    for entry in all_entries:
      response.append(CommitMessage.make(entry))
    return response

  @staticmethod
  def make(entry):
    """Create a new CommitMessage from an individual entry"""
    match = CommitMessage._MEDIUM_PRETTY_COMMIT_MATCHER.match(entry)

    text = entry[match.end(3):]

    # strip trailing spaces on each line
    lines = [line.rstrip() for line in text.split('\n')]

    # remove blank lines from beginning and end of text
    while lines and not lines[0]:
      del lines[0]
    while lines and not lines[-1]:
      del lines[-1]

    # new string may have initial spacing but no leading/trailing blank lines.
    text = '\n'.join(lines)

    return CommitMessage(match.group(1), match.group(2), match.group(3), text)

  @staticmethod
  def normalize_message_list(msg_list):
    """Transform a series of CommitMessage into one without compound messages.

    A compound message is a single commit that is a merge of multiple commits
    as indicated by a message that looks like multiple entires.
    This will break apart those compound commits into individual ones for
    the same commit id entry for easier processing.
    """
    msg_list = CommitMessage._unpack_embedded_commits(msg_list)
    return CommitMessage._unpack_embedded_summaries(msg_list)

  @staticmethod
  def _unpack_embedded_commits(msg_list):
    """Helper function looking for merged commits.

    These are indicated by having an embedded commit within them.
    If found, unnest the indentation and run through as if these
    came directly from a git result to turn them into additional commits.
    """
    result = []
    for commit_message in msg_list:
      text = commit_message.message
      found = CommitMessage._EMBEDDED_COMMIT_MATCHER.search(text)
      if not found:
        result.append(commit_message)
        continue

      text_before = text[:found.start(1)]
      text_lines = text[found.start(1):].split('\n')
      pruned_lines = []
      offset = found.end(1) - found.start(1)
      for line in text_lines:
        if line.startswith(found.group(1)):
          pruned_lines.append(line[offset:])
        elif not line:
          pruned_lines.append(line)
        else:
          logging.warning(
              '"%s" looks like an composite commit, but not indented by %d.',
              text, offset)
          pruned_lines = []
          result.append(commit_message)
          break

      if pruned_lines:
        if text_before.strip():
          logging.info('Dropping commit message "%s" in favor of "%s"',
                       text_before, '\n'.join(pruned_lines))
        result.extend(
            CommitMessage.make_list_from_result('\n'.join(pruned_lines)))
    return result

  @staticmethod
  def _unpack_embedded_summaries(msg_list):
    """Helper function looking for embedded summaries.

    An embedded summary is another top-line message block (e.g. fix(...): ...)
    This is different from a merged commit in that it doesnt have the
    commit/author/date fields. These types of entries are typically entered
    manually to convey a single atomic commit contains multiple changes
    as opposed to multiple commits becomming aggregated after the fact.

    Note that embedded summaries all originated from an atomic commit, so
    all the resulting CommitMessages will have the same underlying commit id.
    """
    result = []
    for commit_message in msg_list:
      commit_id = commit_message.commit_id
      author = commit_message.author
      date = commit_message.date

      lines = commit_message.message.split('\n')
      prev = -1
      for index, line in enumerate(lines):
        if CommitMessage._EMBEDDED_SUMMARY_MATCHER.match(line):
          logging.debug('Found embedded commit at line "%s" prev=%d',
                        line, prev)
          if prev >= 0:
            text = '\n'.join(lines[prev:index]).rstrip()
            result.append(CommitMessage(commit_id, author, date, text))
          prev = index
      if prev < 0:
        prev = 0
      text = '\n'.join(lines[prev:]).rstrip()
      result.append(CommitMessage(commit_id, author, date, text))

    return result

  @staticmethod
  def determine_semver_implication_on_list(
      msg_list, major_regexs=None, minor_regexs=None, patch_regexs=None,
      default_semver_index=SemanticVersion.MINOR_INDEX):
    """Determine the worst case semvar component that needs incremented."""
    if not msg_list:
      return None
    msi = SemanticVersion.PATCH_INDEX + 1
    for commit_message in msg_list:
      msi = min(msi, commit_message.determine_semver_implication(
          major_regexs=major_regexs,
          minor_regexs=minor_regexs,
          patch_regexs=patch_regexs,
          default_semver_index=default_semver_index))
    return msi

  def determine_semver_implication(
      self, major_regexs=None, minor_regexs=None, patch_regexs=None,
      default_semver_index=SemanticVersion.MINOR_INDEX):
    """Determine what effect this commit has on future semantic versioning.

    Args:
      major_regexs: [list of re] Regexes to look for indicating a MAJOR change
      minor_regexs: [list of re] Regexes to look for indicating a MINOR change
      patch_regexs: [list of re] Regexes to look for indicating a PATCH change
      default_semver_index: SemanticVersion.*_INDEX for default change

    Returns:
      The SemanticVersion.*_INDEX of the affected idealized version component
      that will need to be incremented to accomodate this change.
    """
    def is_compliant(spec):
      """Determine if the commit message satisfies the specification."""
      if not spec:
        return None
      if not isinstance(spec, list):
        spec = [spec]

      text = self.message.strip()
      for matcher in spec:
        match = matcher.search(text)
        if match:
          return match.groups()
      return None

    if major_regexs is None:
      major_regexs = CommitMessage.DEFAULT_MAJOR_REGEXS
    if minor_regexs is None:
      minor_regexs = CommitMessage.DEFAULT_MINOR_REGEXS
    if patch_regexs is None:
      patch_regexs = CommitMessage.DEFAULT_PATCH_REGEXS

    attempts = [('MAJOR', major_regexs, SemanticVersion.MAJOR_INDEX),
                ('MINOR', minor_regexs, SemanticVersion.MINOR_INDEX),
                ('PATCH', patch_regexs, SemanticVersion.PATCH_INDEX)]
    for name, regexes, index in attempts:
      reason = is_compliant(regexes)
      if reason:
        logging.debug('Commit is considered "%s" because it says "%s"',
                      name, reason)
        return index

    logging.debug('Commit is considered #%d by DEFAULT: message was "%s"',
                  default_semver_index, self.message)
    return default_semver_index



class RepositorySummary(collections.namedtuple(
    'RepositorySummary',
    ['commit_id', 'tag', 'version', 'commit_messages'])):
  """Denotes information about a repository that a build-delta wants.

  Attributes:
    commit_id: [string] The HEAD commit id
    tag: [string] The tag at the HEAD
    version: [string] The Major.Minor.Patch version number
    commit_messages: [list of CommitMessage] The commits since the last tag.
       If this is empty then the tag and version already exists.
       Otherwise the tag and version are proposed values.
  """
  def to_yaml(self, with_commit_messages=True):
    """Convert the summary to a yaml string."""
    data = dict(super(RepositorySummary, self).__dict__)
    del data['commit_messages']

    if with_commit_messages:
      # Need data['commit_messages'] = [
      #   message.to_dict() for message in self.commit_messages]
      raise NotImplementedError('with_commit_messages not yet supported')

    return yaml.dump(data, default_flow_style=False)


class GitRunner(object):
  """Helper class for interacting with Git"""

  __GITHUB_TOKEN = None

  @staticmethod
  def add_git_parser_args(
      parser, defaults,
      pull=False, push=False, pull_request=False, common=True):
    """Add standard options needed to interact with git for various modes.

    The push/pull_request controls are safety mechanisms to help ensure no
    side effects may happen if use cases desire that.

    The remaining configuration parameters are primarily here on behalf of
    consumers to standardize with. They do not control the runner itself,
    rather what callers of the runner may pass to it.

    Args:
      parer: [argparse.ArgumentParser] The parser to add to.
      defaults: [dict] The default value overrides, keyed by option.
      pull: [boolean] Set to true if git will be used to pull data (e.g. clone)
      push: [boolean] Set to true if git will be used to push data (e.g. push)
      pull_request: [boolean] Set to true if git will be used to initiate PRs.
      common: [boolean] Set to false to disable common args because they were
         already added by another call to this method.
    """
    def add_argument(parser, name, defaults, default_value, **kwargs):
      """Helper function"""
      parser.add_argument(
          '--{name}'.format(name=name),
          default=defaults.get(name, default_value),
          **kwargs)

    if common:
      add_argument(
          parser, 'github_user', defaults, 'default',
          help='Github user repositories to interact with.')
      add_argument(
          parser, 'git_branch', defaults, None,
          help='Github branch to interact with.')

    if pull:
      add_argument(
          parser, 'fallback_branch', defaults, None,
          help='Github branch to pull from if --git_branch is not found.')

    if push:
      pass

    if pull_request:
      add_argument(
          parser, 'pr_notify_list', defaults, None,
          help='Comma-separated list of Github users'
          ' to notify when creating Github pull requests.')
      add_argument(
          parser, 'no_pr', defaults, False,
          help='Do not submit upstream PRs even if pushing to origin.')


  @staticmethod
  def __normalize_repo_url(url):
    """Normalize a repo url for purposes of checking equality.

    Returns:
      Either a tuple (HOST, OWNER, PATH) if url is a github-like URL
         assumed to be in the form <PROTOCOL://HOST/OWNER/PATH> where
         or ssh@<HOST>:<USER><REPO>
         in these cases, a '.git' REPO postfix is considered superfluous.

      Otherwise a string assuming the url is a local path
         where the string will be the absolute path.
    """
    dot_git = '.git'
    gitless_url = (url[:-len(dot_git)]
                   if url.endswith(dot_git)
                   else url)

    # e.g. http://github.com/USER/REPO
    match = re.match(r'[a-z0-9]+://([^/]+)/([^/]+)/(.+)', gitless_url)
    if not match:
      # e.g. git@github.com:USER/REPO
      match = re.match(r'git@([^:]+):([^/]+)/(.+)', gitless_url)
    if match:
      return match.groups()

    return os.path.abspath(url)

  @staticmethod
  def is_same_repo(first, second):
    """Determine if two URLs refer to the same github repo."""
    normalized_first = GitRunner.__normalize_repo_url(first)
    normalized_second = GitRunner.__normalize_repo_url(second)
    return normalized_first == normalized_second

  @staticmethod
  def stash_and_clear_auth_env_vars():
    """Remove git auth variables from global environment; keep internally."""
    if 'GITHUB_TOKEN' in os.environ:
      GitRunner.__GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
      del os.environ['GITHUB_TOKEN']

  @property
  def options(self):
    """Return bound options."""
    return self.__options

  def __init__(self, options):
    self.__options = options
    self.__auth_env = {}
    if GitRunner.__GITHUB_TOKEN:
      self.__auth_env['GITHUB_TOKEN'] = GitRunner.__GITHUB_TOKEN

  def __inject_auth(self, keyword_args_to_modify):
    """Inject the configured git authentication environment variables.

    Args:
      keyword_args_to_modify: [dict]
          The kwargs dictionary that will be passed to the subprocess is
          modified to inject additional authentication variables,
          if configured to do so.
    """
    if not self.__auth_env:
      return
    new_env = dict(keyword_args_to_modify.get('env', os.environ))
    new_env.update(self.__auth_env)
    keyword_args_to_modify['env'] = new_env

  def run_git(self, git_dir, command, **kwargs):
    """Wrapper around run_subprocess."""
    self.__inject_auth(kwargs)
    return run_subprocess(
        'git -C "{dir}" {command}'.format(dir=git_dir, command=command),
        **kwargs)

  def check_git(self, git_dir, command, **kwargs):
    """Wrapper around check_subprocess."""
    self.__inject_auth(kwargs)
    return check_subprocess(
        'git -C "{dir}" {command}'.format(dir=git_dir, command=command),
        **kwargs)

  def check_git_sequence(self, git_dir, commands):
    """Check a sequence of git commands.

    Args:
      git_dir: [path] All the commands refer to this lcoal repository.
      commands: [list of string] To pass to check_git.
    """
    for cmd in commands:
      self.check_git(git_dir, cmd)

  def query_local_repository_commits_to_existing_tag_from_id(
      self, git_dir, commit_id, commit_tags):
    """Returns the list of commit messages to the local repository."""
    # pylint: disable=invalid-name

    id_to_newest_tag = {}
    for commit_tag in sorted(commit_tags):
      id_to_newest_tag[commit_tag.commit_id] = commit_tag.tag
    tag = id_to_newest_tag.get(commit_id)
    if tag is not None:
      return tag, []

    result = self.check_git(git_dir, 'log --pretty=oneline ' + commit_id)
    lines = result.split('\n')
    count = 0
    for line in lines:
      line_id = line.split(' ', 1)[0]
      tag = id_to_newest_tag.get(line_id)
      if tag:
        break
      count += 1

    if tag is None:
      raise ValueError(
          'There is no baseline tag for commit "{id}" in {dir}.'.format(
              id=commit_id, dir=git_dir))

    result = self.check_git(
        git_dir,
        'log -n {count} --pretty=medium {id}'.format(
            count=count, id=commit_id))
    messages = CommitMessage.make_list_from_result(result)
    return tag, messages

  def query_local_repository_commit_id(self, git_dir):
    """Returns the current commit for the repository at git_dir."""
    result = self.check_git(git_dir, 'rev-parse HEAD')
    return result

  def query_local_repository_branch(self, git_dir):
    """Returns the branch for the repository at git_dir."""
    returncode, stdout = self.run_git(git_dir, 'rev-parse --abbrev-ref HEAD')
    if returncode:
      error = 'Could not determine branch: ' + stdout
      raise RuntimeError(error + ' in ' + git_dir)
    return stdout

  def push_branch_to_origin(self, git_dir, branch):
    """Push the given local repository back up to the origin.

    This has no effect if the repository is not in the given branch.
    """
    in_branch = self.query_local_repository_branch(git_dir)
    if in_branch != branch:
      logging.warning('Skipping push %s "%s" to origin because branch is "%s".',
                      git_dir, branch, in_branch)
      return
    self.check_git(git_dir, 'push origin --tags ' + branch)

  def refresh_local_repository(self, git_dir, remote_name, branch):
    """Refreshes the given local repository from the remote one.

    Args:
      git_dir: [string] Which local repository to update.
      remote_name: [remote_name] Which remote repository to pull from.
      branch: [string] Which branch to pull.
    """
    repository = self.determine_remote_git_repository(git_dir)
    if remote_name == 'upstream' and not repository.upstream_url:
      logging.warning(
          'Skipping pull {remote_name} {branch} in {repository} because'
          ' it does not have a remote "{remote_name}"'
          .format(remote_name=remote_name,
                  branch=branch,
                  repository=repository.name))
      return

    local_branch = self.query_local_repository_branch(git_dir)
    if local_branch != branch:
      logging.warning(
          'Skipping pull {remote_name} {branch} in {repository} because'
          ' its in branch={local_branch}'
          .format(remote_name=remote_name,
                  branch=branch,
                  repository=repository.name,
                  local_branch=local_branch))
      return

    try:
      logging.debug('Refreshing %s from %s branch %s',
                    git_dir, remote_name, branch)
      command = 'pull {remote_name} {branch} --tags'.format(
          remote_name=remote_name, branch=branch)
      result = self.check_git(git_dir, command)
      logging.info('%s:\n%s', repository.name, result)
    except RuntimeError:
      result = self.check_git(git_dir, 'branch -r')
      if result.find(
          '{which}/{branch}\n'.format(which=remote_name, branch=branch)) >= 0:
        raise
      logging.warning(
          'WARNING {name} branch={branch} is not known to {which}.\n'
          .format(name=repository.name, branch=branch, which=remote_name))

  def __check_clone_branch(self, origin_url, base_dir, clone_command, branches):
    remaining_branches = list(branches)
    while True:
      branch = remaining_branches.pop(0)
      cmd = '{clone} -b {branch}'.format(clone=clone_command, branch=branch)
      retcode, stdout = self.run_git(base_dir, cmd)
      if not retcode:
        return

      not_found = stdout.find('Remote branch {branch} not found'
                              .format(branch=branch)) >= 0
      if not not_found:
        full_command = 'git -C "{dir}" {cmd}'.format(dir=base_dir, cmd=cmd)
        raise CalledProcessError(cmd=full_command,
                                 returncode=retcode, output=stdout)

      if remaining_branches:
        logging.warning(
            'Branch %s does not exist in %s. Retry with %s',
            branch, origin_url, remaining_branches[0])
        continue

      lines = stdout.split('\n')
      stdout = '\n   '.join(lines)
      logging.error('git -C "%s" %s failed with output:\n   %s',
                    base_dir, cmd, stdout)
      raise ValueError('Branches {0} do not exist in {1}.'
                       .format(branches, origin_url))

  def clone_repository_to_path(
      self, origin_url, git_dir, upstream_url=None,
      commit=None, branch=None, default_branch=None):
    """Clone the remote repository at the given commit or branch.

    If requesting a branch and it is not found, then settle for the default
    branch, if one was explicitly specified.
    """
    # pylint: disable=too-many-arguments

    if (commit != None) and (branch != None):
      raise ValueError('At most one of commit or branch can be specified.')

    logging.debug('Begin cloning %s', origin_url)
    parent_dir = os.path.dirname(git_dir)
    ensure_dir_exists(parent_dir)

    clone_command = 'clone ' + origin_url
    if branch:
      branches = [branch]
      if default_branch:
        branches.append(default_branch)
      self.__check_clone_branch(origin_url, parent_dir, clone_command, branches)
    else:
      self.check_git(parent_dir, clone_command)
    logging.info('Cloned %s into %s', origin_url, parent_dir)

    if commit:
      self.check_git(git_dir, 'checkout -q ' + commit, echo=True)

    if upstream_url and not self.is_same_repo(upstream_url, origin_url):
      logging.debug('Adding upstream %s with disabled push', upstream_url)
      self.check_git(git_dir, 'remote add upstream ' + upstream_url)

    which = ('upstream'
             if upstream_url and not self.is_same_repo(upstream_url, origin_url)
             else 'origin')
    self.check_git(
        git_dir, 'remote set-url --push {which} disabled'.format(which=which))
    logging.debug('Finished cloning %s', origin_url)

  def tag_head(self, git_dir, tag):
    """Add tag to the local repository HEAD."""
    self.check_git(git_dir, 'tag {tag} HEAD'.format(tag=tag))

  def query_tag_commits(self, git_dir, tag_pattern):
    """Collect the TagCommit for each tag matching the pattern.

      Returns: list of CommitTag sorted most recent first.
    """
    tag_ref_result = self.check_git(git_dir, 'show-ref --tags')

    ref_lines = tag_ref_result.split('\n')
    commit_tags = [CommitTag.make(line) for line in ref_lines]
    matcher = re.compile(tag_pattern)
    filtered = [ct for ct in commit_tags if matcher.match(ct.tag)]
    return sorted(filtered, reverse=True)

  def determine_remote_git_repository(self, git_dir):
    """Infter RemoteGitRepository from a local git repository."""
    git_text = self.check_git(git_dir, 'remote -v')
    remote_urls = {
        match.group(1): match.group(2)
        for match in re.finditer(r'(\w+)\s+(\S+)\s+\(fetch\)', git_text)
    }
    origin_url = remote_urls.get('origin')
    if not origin_url:
      raise ValueError('{0} has no remote "origin"'.format(git_dir))

    upstream_url = remote_urls.get('upstream')
    upstream = (RemoteGitRepository.make_from_url(upstream_url)
                if upstream_url
                else None)
    return RemoteGitRepository.make_from_url(origin_url, upstream_ref=upstream)

  def collect_repository_summary(self, git_dir):
    """Collects RepsitorySummary from local repository directory."""
    start_time = time.time()
    logging.debug('Begin analyzing %s', git_dir)
    all_tags = self.query_tag_commits(
        git_dir, r'^version-[0-9]+\.[0-9]+\.[0-9]+$')
    current_id = self.query_local_repository_commit_id(git_dir)
    tag, msgs = self.query_local_repository_commits_to_existing_tag_from_id(
        git_dir, current_id, all_tags)

    current_semver = SemanticVersion.make(tag)
    next_semver = None
    if msgs:
      semver_significance = CommitMessage.determine_semver_implication_on_list(
          msgs)
      next_semver = current_semver.next(semver_significance)
      use_tag = next_semver.to_tag()
      use_version = next_semver.to_version()
    else:
      use_tag = tag
      use_version = current_semver.to_version()

    total_ms = int((time.time() - start_time) * 1000)
    logging.debug('Finished analyzing %s in %d ms', git_dir, total_ms)
    return RepositorySummary(current_id, use_tag, use_version, msgs)

  def reinit_local_repository_with_tag(
      self, git_dir, git_tag, initial_commit_message):
    """Recreate the given local repository using the current content.

    The Netflix Nebula gradle plugin that spinnaker uses to build the sources
    is hardcoded with some incompatible constraints which we are unable to
    change (the owner is receptive but wont do it for us and we arent familiar
    with how it works or exactly what needs changed). Therefore we'll wipe out
    the old tags and just put the one tag we want in order to avoid ambiguity.
    We'll detatch the old repository to avoid accidental pushes. It's much
    faster to create a new repo than remove all the existing tags.

    Args:
      git_dir: [path]  The path to the local git repository.
         If this has an existing .git directory it will be removed.
         The directory will be re initialized as a new repository.
      git_tag: [string] The tag to give the initial commit
      initial_commit_message: [string] The initial commit message
        when commiting the existing directory content to the new repo.
    """
    if git_tag is None:
      git_tag = self.check_git(git_dir, 'describe --tags --abbrev=0')

    escaped_commit_message = initial_commit_message.replace('"', '\\"')
    logging.info('Removing old .git from %s and starting new at %s',
                 git_dir, git_tag)
    original_origin = self.determine_remote_git_repository(git_dir)

    shutil.rmtree(os.path.join(git_dir, '.git'))

    _git = 'git -C "{dir}" '.format(dir=git_dir)
    check_subprocess_sequence([
        _git + 'init',
        _git + 'add .',
        _git + 'remote add {name} {url}'.format(
            name='origin', url=original_origin.url),
        _git + 'remote set-url --push {name} disabled'.format(name='origin'),
        _git + 'commit -q -a -m "{message}"'.format(
            message=escaped_commit_message),
        _git + 'tag {tag} HEAD'.format(tag=git_tag)
    ])

  def delete_local_branch_if_exists(self, git_dir, branch):
    """Delete the branch from git_dir if one exists.

    This will fail if the branch exists and the git_dir is currently in it.
    """
    result = self.check_git(git_dir, 'branch -l')
    branches = []
    for elem in result.split('\n'):
      if elem.startswith('*'):
        elem = elem[1:].strip()
      branches.append(elem)

    if branch in branches:
      logging.info('Deleting existing branch %s from %s', branch, git_dir)
      self.check_git(git_dir, 'branch -D ' + branch)
      return

  def initiate_github_pull_request(
      self, git_dir, message, base='master', head=None):
    """Initialize a pull request for the given commit on the given branch.

    Args:
      git_dir: [path] The local repository to initiate the pull request with.
      message: [string] The pull request message. If this is multiple lines
         then the first line will be the title, subsequent lines will
         be the PR description.
      base: [string] The base reference for the pull request.
         The default is master, but this could be a BRANCH or OWNER:BRANCH
      head: [string] The branch to use for the pull request. By default this
         is the current branch state of the the git_dir repository. This
         too can be BRANCH or OWNER:BRANCH. This branch must have alraedy been
         pushed to the origin repository -- not the local repository.
    """
    options = self.options
    message = message.strip()
    if options.pr_notify_list:
      message.append('\n\n@' + ', @'.join(','.split(options.pr_notify_list)))

    hub_args = []
    if base:
      hub_args.extend(['-b', base])
    if head:
      hub_args.extend(['-h', head])

    if options.no_pr:
      logging.warning(
          'SKIPPING the creation of a pull request because --no_pr.'
          '\nCommand would have been: %s',
          'hub -C "{dir}" pull-request {args} -m {msg!r}'.format(
              dir=git_dir, args=' '.join(hub_args), msg=message))
      return

    message_path = None
    if message.find('\n') < 0:
      hub_args.extend(['-m', message])
    else:
      fd, message_path = tempfile.mkstemp(prefix='hubmsg')
      os.write(fd, message)
      os.close(fd)
      hub_args.extend(['-F', message_path])

    logging.info(
        'Initiating pull request in %s from %s to %s with message:\n%s',
        git_dir, base, head if head else '<current branch>', message)

    try:
      kwargs = {}
      self.__inject_auth(kwargs)
      output = check_subprocess(
          'hub -C "{dir}" pull-request {args}'.format(
              dir=git_dir, args=' '.join(hub_args)),
          **kwargs)
      logging.info(output)
    finally:
      if message_path:
        os.remove(message_path)
