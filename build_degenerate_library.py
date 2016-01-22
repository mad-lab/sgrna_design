#!/usr/bin/env python

# Author: John Hawkins (jsh) [really@gmail.com]

import argparse
import bisect
import collections
import contextlib
import copy
import itertools
import logging
import os.path
import pdb
import random
import re
import shutil
import subprocess
import string
import sys
import tempfile

from Bio import SeqIO
import pysam

from sgrna_target import sgrna_target
import degenerate_variants as degvar


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

class Error(Exception):
  pass

class SampleError(Error):
  pass


DNA_PAIRINGS = string.maketrans('atcgATCG', 'tagcTAGC')

def revcomp(x):
  return x.translate(DNA_PAIRINGS)[::-1]


def extract_targets(infile_name, pam, target_len):
  """Generate the complete list of pam-adjacent potential targets in a genome.

  Args:
    infile_name [str]:  Name of the file containing the source genome.
    pam [str]:          Regexp DNA pattern for the PAM sequence.
    target_len [int]:   How many bases to pull from the adjacent region.
  Returns:
    Iterable sequence of sgrna targets.
  Notes:
    Discards targets containing 'N' bases.
  """
  # TODO(jsh): Do something with "bases" other than N, ATCG.
  logging.info('Extracting target set from {infile_name}.'.format(**vars()))
  fasta_sequences = SeqIO.parse(infile_name, 'fasta')
  raw_targets = list()
  for seq_record in fasta_sequences:
    genome = seq_record.seq.upper()
    chrom = seq_record.name
    pam = pam.upper()
    reversed_pam = revcomp(pam)
    block = r'(.{' + str(target_len) + r'})'
    pam_pattern = r'(?=(' + block + pam + r'))'
    rev_pattern = r'(?=(' + reversed_pam + block + r'))'
    for hit in re.finditer(pam_pattern, str(genome)):
      if 'N' in hit.group(1):
        continue  # ...Don't target unknown genetic material.
      t = sgrna_target(
                hit.group(2),
                hit.group(1)[-len(pam):],
                chrom,
                hit.start() + 1,
                hit.start() + 1 + target_len,
                False)
      name = t.id_str()
      raw_targets.append((name, t))
    for hit in re.finditer(rev_pattern, str(genome)):
      if 'N' in hit.group(1):
        continue
      t = sgrna_target(
                revcomp(hit.group(2)),
                revcomp(hit.group(1))[-len(pam):],
                chrom,
                hit.start() + 1 + len(pam),
                hit.start() + 1 + len(pam) + target_len,
                True)
      name = t.id_str()
      raw_targets.append((name, t))
  logging.info('{0} raw targets.'.format(len(raw_targets)))
  return raw_targets


def parse_target_regions(target_regions_file):
  """Extract target regions into a more usable form.

  Args:
    target_regions_file: Name of input file.
  Returns:
    target_regions: list of (gene, chrom, start, end, strand) entries.
  """
  logging.info('Parsing target region file.')
  target_regions = list()
  for x in open(target_regions_file):
    if x.startswith('#'):
      continue
    parts = x.strip().split('\t')
    try:
      (name,chrom,start,end,strand) = parts
    except ValueError:
      trf = target_regions_file
      logging.error('Could not parse from {trf}: {x}'.format(**vars()))
      sys.exit(1)
    try:
      target_regions.append((name, chrom, int(start), int(end), strand))
    except ValueError:
      x = x.strip()
      logging.warning('Could not fully parse: {x}'.format(**vars()))
      continue
  logging.info(
      'Found {0} target regions in region file.'.format(len(target_regions)))
  return target_regions


def chrom_lengths(fasta_file_name):
  """Get lengths of chromosomes (entries) for fasta file.

  Args:
    fasta_file_name [str]:  Name of the file containing the source genome.
  Returns:
    chrom_lens: dict mapping fasta entry name (chrom) to sequence length.
  """
  logging.info('Parsing fasta file to check chromosome sizes.')
  chrom_lens = dict()
  fasta_sequences = SeqIO.parse(fasta_file_name, 'fasta')
  for seq_record in fasta_sequences:
    chrom_lens[seq_record.name] = len(seq_record.seq)
  return chrom_lens


def ascribe_specificity(targets, genome_fasta_name, sam_copy):
  """Set up bowtie stuff and repeatedly call mark_specificity_tier."""
  # Is there a bowtie index yet?
  if not os.path.exists(genome_fasta_name + '.1.ebwt'):
    command = ['bowtie-build', genome_fasta_name, genome_fasta_name]
    build_job = subprocess.Popen(command)
    if build_job.wait() != 0:
      logging.fatal('Failed to build bowtie index')
      sys.exit(build_job.returncode)
  # Generate faked FASTQ file
  phredString = '++++++++44444=======!4I'  # 33333333222221111111NGG
  # for threshold in (95,90,80,70,60,50,40,30,20,11,1):
  for threshold in (60,50,40,30,20,11,1):
    fastq_tempfile, fastq_name = tempfile.mkstemp()
    with contextlib.closing(os.fdopen(fastq_tempfile, 'w')) as fastq_file:
      for name, t in targets.iteritems():
        if t.specificity > 0:
          continue
        fullseq = t.sequence_with_pam()
        fastq_file.write(
            '@{name}\n{fullseq}\n+\n{phredString}\n'.format(**vars()))
    mark_unadjusted_specificity_threshold(
        targets, fastq_name, genome_fasta_name, threshold, sam_copy)
  for _, t in targets.iteritems():
    if t.specificity != 0:
      t.specificity -= t.weakness

def mark_unadjusted_specificity_threshold(
        targets, fastq_name, genome_name, threshold, sam_copy):
  """Marks the indicated threshold for any targets that are newly lapsed.

  'Unadjusted' because it does not account for a non-zero relative weakness
  of the guide to the intended target.
  """
  # prep output files
  (specific_tempfile, specific_name) = tempfile.mkstemp()
  # Filter based on specificity
  command = ['bowtie']
  command.extend(['-S'])  # output SAM
  command.extend(['--nomaqround'])  # don't do rounding
  command.extend(['-q'])  # input is fastq
  command.extend(['-a'])  # report each non-specific hit
  command.extend(['--best'])  # judge the *closest* non-specific match
  command.extend(['--tryhard'])  # judge the *closest* non-specific match
  command.extend(['--chunkmbs', 256])  # memory setting for --best flag
  command.extend(['-p', 6])  # how many processors to use
  command.extend(['-n', 3])  # allowable mismatches in seed
  command.extend(['-l', 15])  # size of seed
  command.extend(['-e', threshold])  # dissimilarity sum before not non-specific hit
  command.extend(['-m', 1])  # discard reads with >1 alignment
  command.append(genome_name)  # index base, built above
  command.append(fastq_name)  # faked fastq temp file
  command.append(specific_name)  # unique hits
  command = [str(x) for x in command]
  logging.info(' '.join(command))
  bowtie_job = subprocess.Popen(command)
  # Check for problems
  if bowtie_job.wait() != 0:
    sys.exit(bowtie_job.returncode)
  if sam_copy:
    shutil.copyfile(specific_name, sam_copy)
  aligned_reads = pysam.Samfile(specific_name)
  for x in aligned_reads:
    # flag 4 means unaligned, so skip those
    if not x.flag & 4:
      t = targets[x.qname]
      if t.specificity < threshold:
        t.specificity = threshold
  os.close(specific_tempfile)


def label_targets(targets,
                  target_regions,
                  chrom_lens,
                  include_unlabeled,
                  allow_partial_overlap):
  """Annotate targets according to overlaps with gff entries.
  Args:
    targets: the targets to annotate.
    target_regions: the target regions for which to produce annotations
    chrom_lens: mapping from chrom name to sequence length.
    include_unlabeled: if true, add unlabeled versions of full target set.
    allow_partial_overlap: Include targets which only partially overlap region.
  Returns:
    anno_targets: list of targets with added region annotations
  """
  logging.info(
      'Labeling targets based on region file.'.format(**vars()))
  anno_targets = list()
  found = set()
  counter = 0
  # Organize targets by chromosome and then start location.
  per_chrom_sorted_targets = collections.defaultdict(list)
  for name, x in targets.iteritems():
    per_chrom_sorted_targets[x.chrom].append(x)
  for x in per_chrom_sorted_targets:
    per_chrom_sorted_targets[x].sort(key=lambda x:(x.start, x.end))
  per_chrom_bounds = dict()
  for chrom in chrom_lens:
    per_chrom_bounds[chrom] = (0,0) # Check out the bound variables
  for i, x in enumerate(target_regions):
    (gene, chrom, gene_start, gene_end, gene_strand) = x
    # if gene in ('rfp', 'gfp'):
    #   pdb.set_trace()
    if i % 100 is 0:
      logging.info('Examining gene {i} [{gene}].'.format(**vars()))
    front, back = per_chrom_bounds[chrom]
    reverse_strand_gene = gene_strand == '-'
    if gene_start >= chrom_lens[chrom]:
      continue
    chrom_targets = per_chrom_sorted_targets[chrom]
    # TODO(jsh): If a gene is contained within another gene, we might double
    # label outer-gene guides that are later than the end of the inner gene
    if allow_partial_overlap:
      # Shift back index until target.start >= gene_end
      while (back < len(chrom_targets) and
             chrom_targets[back].start < gene_end):
        back += 1
      # Shift front index until target.end > gene_start
      while (front < len(chrom_targets) and
             chrom_targets[front].end <= gene_start):
        front += 1
    else:
      # Shift back index until target.end > gene_end
      while (back < len(chrom_targets) and
             chrom_targets[back].end <= gene_end):
        back += 1
      # Shift front index until target.start >= gene_start
      while (front < len(chrom_targets) and
             chrom_targets[front].start < gene_start):
        front += 1
    overlap = chrom_targets[front:back]
    per_chrom_bounds[chrom] = (front, back) # Return bound vars to shelf
    # TODO(jsh): maybe change this warning to play nicely with chunks somehow
    # if len(overlap) == 0:
    #   logging.warn('No overlapping targets for gene {gene}.'.format(**vars()))
    for target in overlap:
      found.add(target.id_str())
      if reverse_strand_gene:
        offset = gene_end - target.end
      else:
        offset = target.start - gene_start
      returnable = copy.deepcopy(target)
      returnable.gene = gene
      returnable.offset = offset
      returnable.sense_strand = (reverse_strand_gene == target.reverse)
      anno_targets.append(returnable)
  if include_unlabeled:
    for name, target in targets.iteritems():
      if name not in found:
        anno_targets.append(copy.deepcopy(target))
  return anno_targets


def parse_manual_targets(manual_target_file):
  logging.info('Parsing manual target file.')
  manual_targets = list()
  for x in open(manual_target_file):
    name, target, pam = x.strip().split('\t')
    t = sgrna_target(
        target,
        pam,
        name,
        0,
        0,
        False)
    manual_targets.append((t.id_str(), t))
  return manual_targets


def parse_args():
  """Read in the arguments for the sgrna library construction code."""
  logging.info('Parsing command line.')
  parser = argparse.ArgumentParser(
      formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--input_fasta_genome_name', type=str, required=True,
                      help='Location of genome file in FASTA format.')
  parser.add_argument('--manual_target_set', type=str, required=False,
                      help='Location of manual target list.')
  parser.add_argument('--sam_copy', type=str,
                      help='Copy of sam file from (final) bowtie run.',
                      default=None)
  parser.add_argument('--tsv_file_name', type=str,
                      help='Output file to create.', default=None)
  parser.add_argument('--target_regions_file', type=str, required=True,
                      help='Location of target regions file in tsv format.')
  parser.add_argument('--include_unlabeled', action='store_true',
      default=False,
      help='Output targets even if they overlapped no target region.')
  parser.add_argument('--only_include_fully_overlapping', action='store_false',
      dest='allow_partial_overlap', default=True,
      help='Only label targets which are fully contained in the region.')
  # TODO(jsh) Before using different PAMs, need phred faking flag for PAM.
  parser.add_argument('--pam', default='.gg', type=str,
                      help='NOT YET IMPLEMENTED DO NOT USE!')
  parser.add_argument('--chunk_size', default=1000, type=int, help='')
  parser.add_argument('--double_variants', default=20, type=int,
                      help='How many random samples of double-degenerate variants to use.')
  # TODO(jsh) Need to take correct fraction of phred-score string to unbreak.
  parser.add_argument('--target_len', default=20, type=int,
                      help='NOT YET IMPLEMENTED DO NOT USE!')
  args = parser.parse_args()
  if args.tsv_file_name is None:
    base = os.path.splitext(args.input_fasta_genome_name)[0]
    args.tsv_file_name =  base + '.targets.all.tsv'
  return args


def main():
  args = parse_args()
  # Build initial list
  if args.manual_target_set is not None:
    clean_targets = parse_manual_targets(args.manual_target_set)
  else:
    clean_targets = extract_targets(args.input_fasta_genome_name,
                                    args.pam,
                                    args.target_len)
  chrom_lens = chrom_lengths(args.input_fasta_genome_name)
  # with open('/tmp/original_targets.tsv', 'w') as debug_out:
  #   for target in clean_targets:
  #     debug_out.write(str(target) + '\n')
  # print chrom_lens
  # sys.exit(1)

  # Spawn degenerate variants
  counter = [0]
  with open(args.tsv_file_name, 'w') as tsv_file:
    def process_chunk(chunk):
      chunk_targets=dict()
      for name, t in chunk:
        counter[0] += 1
        if random.random() < 0.001:
          logging.info('Varying target {0}: {1}'.format(counter[0], name))
        chunk_targets[name] = t
        for variant, weakness in degvar.all_single_variants(t.target):
          new_target = copy.deepcopy(t)
          new_target.target = variant
          new_target.weakness = weakness
          name = new_target.id_str()
          chunk_targets[name] = new_target
        i = args.double_variants
        # TODO(jsh): don't ignore indices!
        for variant, weakness in degvar.random_variants(t.target, _, 2):
          if i == 0:
            break
          new_target = copy.deepcopy(t)
          new_target.target = variant
          new_target.weakness = weakness
          name = new_target.id_str()
          # Make sure we actually get args.double_variants worth
          if name in chunk_targets:
            continue
          else:
            i -= 1
          chunk_targets[name] = new_target
      logging.info('{0} all targets.'.format(len(chunk_targets)))
      # Score list
      ascribe_specificity(
          chunk_targets,
          args.input_fasta_genome_name,
          args.sam_copy)
      # Annotate list
      if args.manual_target_set is None:
        target_regions = parse_target_regions(args.target_regions_file)
        chunk_targets = label_targets(chunk_targets,
                                      target_regions,
                                      chrom_lens,
                                      args.include_unlabeled,
                                      args.allow_partial_overlap)
      else:
        chunk_targets = [x for n,x in chunk_targets.iteritems()]
      # Generate output
      total_count = len(chunk_targets)
      logging.info(
          'Writing {total_count} annotated targets to {args.tsv_file_name}'.format(
              **vars()))
      for target in chunk_targets:
        tsv_file.write(str(target) + '\n')

    tsv_file.write('#' + clean_targets[0][1].header() + '\n')
    for start in range(0, len(clean_targets), args.chunk_size):
      process_chunk(clean_targets[start:start+args.chunk_size])

##############################################
if __name__ == "__main__":
  sys.exit(main())
