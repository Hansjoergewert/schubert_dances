# %% Run this cell with ALT + SHIFT + ENTER
"""MuseScore3 Parser"""

###################
#Internal libraries
###################
import os, re, argparse, logging
from collections import defaultdict, Counter
from fractions import Fraction as frac


###################
#External libraries
###################
from bs4 import BeautifulSoup as bs         # python -m pip install beautifulsoup4, lxml
import pandas as pd
import numpy as np

###########
# Constants
###########
DURATIONS = {"measure" : 1.0,
             "breve"   : 2.0,
             "whole"   : 1.0,
             "half"    : frac(1/2),
             "quarter" : frac(1/4),
             "eighth"  : frac(1/8),
             "16th"    : frac(1/16),
             "32nd"    : frac(1/32),
             "64th"    : frac(1/64),
             "128th"   : frac(1/128)}

NEWEST_MUSESCORE = '3.3.0'

NL = '\n'

PITCH_NAMES = {0: 'F',
               1: 'C',
               2: 'G',
               3: 'D',
               4: 'A',
               5: 'E',
               6: 'B'}

class SliceMaker(object):
    """ This class serves for passing slice notation such as :3 as function arguments.
    Example
    -------
        SL = SliceMaker()
        some_function( slice_this, SL[3:8] )"""
    def __getitem__(self, item):
        return item

SL, SM = SliceMaker(), SliceMaker()

TIMESIG_BEAT = {
                '3/16': '1/16',
                '6/16': '3/16',
                '3/8':  '1/8',
                '4/8':  '1/4',
                '6/8':  '3/8',
                '9/8':  '3/8',
                '12/8': '3/8',
                '2/4':  '1/4',
                '3/4':  '1/4',
                '4/4':  '1/4',
                '6/4':  '3/4',
                '2/2':  '1/2',
                '3/2':  '1/2',
                }

# XML tags of MuseScore 3 format that this parser takes care of
TREATED_TAGS = ['acciaccatura',
                'accidental',   # within <KeySig>
                'Accidental',   # within <Note>, ignored
                'actualNotes',  # within <Tuplet>
                'appoggiatura',
                'Articulation', # optional
                'baseNote',     # within <Tuplet>, ignored
                'BarLine',
                'Chord',
                'dots',
                'durationType',
                'endRepeat',
                'endTuplet',
                'fractions',    # ignored (part of Spanner)
                'grace4','grace4after','grace8','grace8after','grace16','grace16after',
                'grace32','grace32after','grace64','grace64after',
                'irregular',    # measure exluded from bar count
                'LayoutBreak',  # subtype 'section' taken into account for repeat structure
                'location',     # within <Volta>
                'Measure',
                'measures',     # within <next> within <Volta>
                'next',         # within <Volta>
                'noOffset',     # vlue to add to bar count from here on
                'normalNotes',  # within <Tuplet>
                'Note',         # within <Chord>
                'Number',       # within <Tuplet>, ignored
                'pitch',
                'prev',         # within <Volta>, ignored
                'Rest',
                'Slur',         # ignored
                'Spanner',      # several cases; used: "Tie" (test 8va)
                'startRepeat',
                'subtype',      # as part of <Articulation> or <BarLine>
                'Tie',          # see Spanner
                'TimeSig',
                'tpc',          # Tonal pitch class C = 0, F = -1, Bb = -2, G = 1,
                                # D = 2 etc. (i.e. MuseScore format minus 14: https://musescore.org/en/plugin-development/tonal-pitch-class-enum)
                'Tuplet',
                'visible',      # ignored
                'voice',
                'Volta']


################################################################################
#                     HELPER FUNCTIONS in alphabetical order
################################################################################
def a_n_range(c, n):
    """Generates character `a` and the `n-1` following characters.
    Parameters
    ----------
    c : :obj:`char`
        Start character.
    n : :obj:`int`
        Number of total characters.

    Example
    -------
    >>> list(a_n_range('b', 4))
    ['b', 'c', 'd', 'e']
    """
    c = ord(c)
    for a in range(c, c+n):
        yield chr(a)



def check_measure_boundaries(notes, measure_durations):
    """ Check that no note surpasses the barline and log errors.

    Parameters
    ----------
    notes : :obj:`pandas.DataFrame`
        DataFrame with columns ['mc', 'onset', 'duration']
    measure_durations : :obj:`pandas.Series`
        A series where the index matches notes.mc

    Returns
    -------
    `None`
    """
    OK = True
    for ix, mc, onset, duration in notes[['mc', 'onset', 'duration']].itertuples():
        if onset + duration > measure_durations.loc[mc]:
            OK = False
            try:
                ix = int(ix) # single index
            except:
                ix = ix[-1]  # multiindex
            logging.warning(f"Event {ix} in MC {mc} has has duration {duration} and starts on {onset}, surpassing the measure length of {measure_durations.loc[mc]}")
    if OK:
        logging.debug("Measure boundaries checked: No errors.")



def check_mn(mn_series):
    """Check measure numbers for gaps and overlaps and logs errors.

    Parameters
    ----------
    mn_series : :obj:`pandas.Series`
        Series of measure numbers.

    Returns
    -------
    `None`
    """
    # Check that numbers are strictly ascending
    ensure_ascending = mn_series < mn_series.shift()
    if ensure_ascending.any():
        ixs = mn_series.index[ensure_ascending]
        logging.error(f"Score contains descending barnumbers at measure count{'s ' if len(ixs) > 1 else ' '}{', '.join([str(i) for i in ixs])}, possibly caused by MuseScore's 'Add to bar number' function.")
    # Check for numbering gaps
    highest = mn_series.max()
    missing = [i for i in range(1, highest) if not i in mn_series.values]
    if len(missing) > 0:
        logging.error(f"The score has a numbering gap, these measure numbers are missing: {missing}")



def compute_mn(df, check=True):
    """Df with first column for excluded measures and facultative second column
       for offset ("add to bar count"); measure counts in the index.

    Example
    -------
        >>> df
        	dont_count	numbering_offset
        0	NaN	        NaN
        1	1	        NaN
        2	NaN	        NaN
        3	NaN	        -1
        4	NaN	        NaN
        >>> compute_mn(df)
        0    1
        1    1
        2    2
        3    2
        4    3
    """
    if df.__class__ == pd.core.series.Series:
        excluded = df
        offset = None
    else:
        excluded = df.iloc[:,0]
        offset   = df.iloc[:,1] if len(df.columns) > 1 else None

    ix = df.index
    regular_ix = ix[excluded.isna()]
    mn = pd.Series(range(1, 1 + len(regular_ix)), index=regular_ix)
    mn = mn.reindex(ix)
    if isnan(mn[0]):   # if anacrusis
        mn[0] = 0
    mn.fillna(method='ffill', inplace=True)
    if offset is not None and offset.notna().any():
        if isnan(offset[0]):
            offset[0] = 0
        offset = offset.cumsum().fillna(method='ffill')
        mn += offset
    mn = mn.astype('int')
    if check:
        check_mn(mn)
    return mn



def compute_repeat_structure(mc_repeats_volta):
    """
    Parameters
    ----------
    df : :obj:`pandas.DataFrame`
        Needs to have the two columns ['repeats', 'volta'] where for every measure count
        in the index a tag {'startRepeat', 'endRepeat', 'firstMeasure', 'lastMeasure'}
        and/or a volta number (typically {1, 2, 3}) is given.

    Returns
    -------
    :obj:`list` of :obj:`tuple` of :obj:`int`
        Beginning and ending measure counts of repeated sections.

    Example
    -------
        >>> df
                repeats	        volta
        0	firstMeasure	 NaN
        16	NaN	            1
        17	endRepeat	    1
        18	NaN	            2
        19	startRepeat	    NaN
        23	endRepeat	    1
        24	endRepeat	    2
        25	NaN	            3
        31	startRepeat	    NaN
        39	endRepeat	    1
        40	NaN	            2
        >>> compute_repeat_structure(df)
        [(0, 18), (19, 25), (31, 40)]
    """
    df = mc_repeats_volta[['repeats', 'volta']].reset_index() # -> 3 columns: [indexname, 'repeats', 'volta']
    df = df[df.repeats.notna() | df.volta.notna()]
    last_row = df.iloc[-1]
    if last_row.repeats == 'lastMeasure' and isnan(last_row.volta):
        df = df.iloc[:-1]
    if len(df) == 1:
        return []
    # Check whether beginning is an implicit startRepeat
    if df.iloc[0,1] == 'firstMeasure':
        i = 1
        while i < len(df)-1 and isnan(df.iloc[i,1]):
            i += 1
        if df.iloc[i,1] == 'endRepeat':
            df.iloc[0,1] = 'startRepeat'
        else:
            df.drop(index=0, inplace=True)


    startRepeats = df.repeats == 'startRepeat'
    start_mcs = df.iloc[:,0][startRepeats].to_list() # measure counts of startRepeats
    endRepeats = startRepeats.shift(-1)
    endRepeats.iloc[-1] = True
    end_mcs = df.iloc[:,0][endRepeats].to_list()
    return list(zip(start_mcs, end_mcs))


def convert_timesig(tag):
    """Turns a TimeSig-tag into a fraction. If you pass a list of tags, all need
    to represent the same time signature. In case of errors, None is returned.

    Parameters
    ----------
    tag : :class:`bs4.element.Tag` (or :obj:`list`)

    Returns
    -------
    :obj:`fractions.Fraction`
    """
    if tag.__class__ == list:
        res = set([convert_timesig(t) for t in tag])
        if len(res) == 0:
            logging.error("List of timesignatures did not yield any result.")
            return None
        elif len(res) > 1:
            logging.error("List contains two different time signatures.")
            return None
        else:
            return res.pop()
    elif tag.name == 'TimeSig':
        N = tag.find('sigN')
        if N:
            N = N.string
        else:
            logging.error("TimeSig tag has no sigN tag.")
            return None
        D = tag.find('sigD')
        if D:
            D = D.string
        else:
            logging.error("TimeSig tag has no sigD tag.")
            return None

        return f"{N}/{D}"
    elif tag.find('TimeSig'):
        return convert_timesig(tag.find('TimeSig'))
    else:
        raise ValueError("Does not contain <TimeSig> tag.")



def feature_from_node(tag, nodes):
    """Gets a tag name and a list of corresponding tags and
    computes a useful value from it.

    Parameters
    ----------
    tag : :obj:`str`
        Tag name
    nodes : :obj:`list` of :class:`bs4.element.Tag` (or :obj:`list`)
        Tags from which to extract values.
    """
    if len(nodes) == 0:
        logging.error("""Got empty node list. This shouldn't have happened:
check construction of defaultdict 'infos' in function get_measure_infos""")
        return None
    if len(nodes) > 1 and tag != 'voice':
        logging.warning(f"{len(nodes)} {tag}-nodes in one <Measure>.")

    if tag == 'voice':
        return len(nodes)
    else:
        node = nodes[0]

    if tag in ['accidental', 'noOffset', 'irregular']:
        try:
            return int(node.string)
        except:
            print(node)
    elif tag == 'TimeSig':
        return convert_timesig(node)
    elif tag in ['endRepeat', 'startRepeat']:
        return tag
    elif tag == 'Volta':
        loc = node.find_next('next').location
        val = 1 if loc.find('fractions') else 0
        if loc.find('measures'):
            val += int(loc.measures.string)
        if val == 0:
            logging.error(f"Length of volta {node} not specified.")
        return val
    elif tag == 'BarLine':
        subtype = node.find('subtype')
        return subtype.string if subtype else 'other'
    else:
        logging.error(f"Treatment of {tag}-tags not implemented.")



def get_volta_structure(df):
    """
    Parameters
    ----------
    df : :obj:`pandas.DataFrame`
        Needs to have the two columns ['repeats', 'volta'] where for every measure count
        in the index a tag {'startRepeat', 'endRepeat', 'firstMeasure', 'lastMeasure'}
        and/or a volta number (typically {1, 2, 3}) is given.

    Returns
    -------
    :obj:`list` of :obj:`list` of :obj:`list` of :obj:`int`
        For every volta group, one list of integers per volta containing the measure
        counts that this volta spans.
    """
    OK = True
    repeats = df.repeats
    voltas = df.volta.dropna()
    volta_structure = []
    nxt = -1
    for i, length in voltas.iteritems():
        volta_range = list(range(i, i + int(length)))
        overlaps = [mc for mc in volta_range if mc in voltas.index[voltas.index>i]]
        if len(overlaps) > 0:
            plural = 1 if len(overlaps) > 1 else 0
            logging.warning(f"Voltas overlap in MC{'s' if plural else ''} {overlaps}")
            volta_range = [mc for mc in volta_range if mc not in overlaps]
        if i != nxt:    # new volta group
            volta_structure.append([volta_range])
        else:           # extend volta group
            volta_structure[-1].append(volta_range)
        nxt = i+len(volta_range)
        if 'startRepeat' in repeats.loc[volta_range,].values:
            logging.error(f"Volta with range {volta_range} contains startRepeat!")
            OK = False

    # Check if voltas in same group have the same length
    for group in volta_structure:
        I = iter(group)
        l = len(next(I))
        if not all(len(volta_range) == l for volta_range in I):
            except_first = sum(group[1:],[])
            if df.dont_count.loc[except_first].isna().any():
                logging.warning(f"""Voltas with measure COUNTS {group} have different lengths.
Check measure NUMBERS with authoritative score. To silence the warning, either make all voltas
the same length or exclude all measures in voltas > 1 from the bar count.""")
                OK = False

    if OK:
        logging.debug("Volta structure OK.")
    return volta_structure


def isnan(num):
    """Return True if `num` is numpy.nan (not a number)"""
    return num != num


def nan_eq(a, b):
    """Check for equality, including NaNs.

    Parameters
    ----------
    a, b : Values to compare or :obj:`pandas.Series` of values to compare.

    Returns
    -------
    :obj:`bool` or :obj:`pandas.Series` of :obj:`bool`
    """
    if a.__class__ == pd.core.series.Series or b.__class__ == pd.core.series.Series:
        assert a.__class__ == pd.core.series.Series, f"If b is a Series, a should not be a {type(b)}"
        assert b.__class__ == pd.core.series.Series, f"If a is a Series, b should not be a {type(b)}"
        return (a == b) | ((a != a) & (b != b))
    return a == b or (isnan(a) and isnan(b))



def midi2octave(val):
    """Returns 4 for values 60-71 and correspondingly for other notes.

    Parameters
    ----------
    val : :obj:`int` or :obj:`pandas.Series` of `int`
    """
    return val // 12 - 1


def search_in_list_of_tuples(L, pos, search, add=0):
    """ Returns a list of indices for tuples that contain `search` at position `pos`.

    Parameters
    ----------
    L : :obj:`list` of :obj:`tuple`
        List of tuples in which you want elements with value `search`.
    pos : :obj:`int`
        In which position of the tuples to search.
    search : :obj:`object`
        What to look for.
    add : :obj:`int`, opt
        How much you want to add to each returned index value.
    """
    return [i+add for i, item in enumerate(L) if item[pos] == search]



def sort_dict(D):
    """ Returns a new dictionary with sorted keys. """
    return {k: D[k] for k in sorted(D)}



def spell_tpc(tpc):
    """Return name of a tonal pitch class where
       0 = C, -1 = F, -2 = Bb, 1 = G etc.
    """
    if tpc.__class__ == pd.core.series.Series:
        return tpc.apply(spell_tpc)
    if tpc.__class__ in [np.ndarray, list, tuple, set]:
        result = [spell_tpc(pc) for pc in tpc]
        if tpc.__class__ == np.ndarray:
            return np.array(result).reshape(tpc.shape)
        if tpc.__class__ == tuple:
            return tuple(result)
        if tpc.__class__ == set:
            return set(result)
        return result

    tpc += 1 # to make the lowest name F = 0 instead of -1
    if tpc < 0:
        acc = abs(tpc // 7) * 'b'
    else:
        acc = tpc // 7 * '#'
    return PITCH_NAMES[tpc % 7] + acc



def treat_section_index(i, n):
    """Check whether the index exists and convert negative index.

    Parameters
    ----------
    i: (`collection` of) :obj:`int`
        Section index/indices to treat.
    n: :obj:`int`:
        Number of available sections.
    """
    try:
        int(i)
    except:
        try:
            treated = [treat_section_index(e, n) for e in i]
        except:
            raise ValueError(f"Error in input {i}.")
        if i.__class__ == tuple:
            return tuple(treated)
        else:
            return treated
    if i < 0:
        if i < -n:
            logging.warning(f"Section {i} does not exist.")
            return None
        else:
            return n + i
    else:
        if i > n-1:
            logging.warning(f"Section {i} does not exist.")
            return None
        return i



################################################################################
#                             SECTION CLASS
################################################################################
class Section(object):
    """ Holds the properties of a section.

    Attributes
    ----------
    first_mc, last_mc : :obj:`int`
        Measure counts of the section's first and last measure nodes.
    first_mn, last_mn : :obj:`int`
        First and last measure number as shown in the score.
    start_break, end_break : :obj:`str`
        What causes the section breaks at either side.
    index : :obj:`int`
        Index (running number) of this section.
    notes : :obj:`pandas.DataFrame`
        DataFrame holding all notes and their features.
    parent : :obj:`Score`
        The parent `Score` object that is creating this section.
    previous_section, next_section : :obj:`int`
        Indices of the previous and following sections in the score.
    repeated : :obj:`bool`
        Whether or not this section is repeated.
    subsection_of : :obj:`int`
        If section is a subsection, the index of the super_section, None otherwise.
    voltas : :obj:`list` of :obj:`range`
        Ranges of voltas. Default: empty list
    """

    def __init__(self, parent, first_mc, last_mc, index, repeated, start_break, end_break, voltas=[]):
        self.first_mc, self.last_mc = first_mc, last_mc
        self.first_mn, self.last_mn = None, None
        self.index = index
        self.repeated = repeated
        self.start_break, self.end_break = start_break, end_break
        self.voltas = [] if voltas is None else voltas
        self.subsection_of = None
        features = ['mc', 'mn', 'onset', 'duration', 'gracenote', 'nominal_duration', 'scalar', 'tied', 'tpc', 'midi', 'staff', 'voice', 'volta']
        for f in ['articulation']:
            if f in parent.score_features:
                features.append(f)
        self.notes = pd.DataFrame(columns=features)
        if index > 0:
            self.previous_section = index-1
            parent.sections[index-1].next_section = index
        else:
            self.previous_section = None
        self.next_section = None


        # Parse all measures contained in this section
        df_vals = {col: [] for col in self.notes.columns}
        # iterate through stacks of simultaneous measure nodes
        for mc, measure_stack in enumerate(zip(*[[measure for mc, measure in node_dicts.items() if self.first_mc <= mc <= self.last_mc] for node_dicts in parent.measure_nodes.values()])):
            mc += self.first_mc
            nodetypes = defaultdict(list)   # keeping track of tags on the measure level
            mc_info = parent.info.loc[mc]
            volta = mc_info.volta
            for staff_id, measure in enumerate(measure_stack):
                staff_id += 1
                for tag in measure.find_all(recursive=False):
                    nodetypes[tag.name].append(tag)
                tagtypes = set()            # keeping track of tags on the event group level
                if 'voice' in nodetypes:
                    # Parse all events within a voice within a measure within a staff
                    for voice, voice_tag in enumerate(nodetypes['voice']):
                        voice += 1
                        pointer = frac(0)
                        scalar = 1  # to manipulate note durations
                        scalar_stack = []
                        for event in voice_tag.find_all(['Chord', 'Rest', 'Tuplet', 'endTuplet']):
                            for tag in event.find_all(recursive=True):
                                tagtypes.add(tag.name)
                            if event.name == 'Tuplet':
                                scalar_stack.append(scalar)
                                scalar = scalar * frac(int(event.normalNotes.string), int(event.actualNotes.string))
                            elif event.name == 'endTuplet':
                                scalar = scalar_stack.pop()
                            else:
                                nominal_duration = DURATIONS[event.find('durationType').string]
                                dots = event.find('dots')
                                dotscalar = sum([frac(1/2) ** i for i in range(int(dots.string)+1)]) * scalar if dots else scalar
                                duration = nominal_duration * dotscalar
                                if event.name == 'Chord':

                                    if 'articulation' in parent.score_features and event.find('Articulation'):
                                        articulation = event.Articulation.subtype.string
                                    else:
                                        articulation = np.nan

                                    grace = event.find(['grace4','grace4after','grace8','grace8after','grace16','grace16after','grace32','grace32after','grace64','grace64after', 'appoggiatura', 'acciaccatura'])
                                    gracenote = grace.name if grace else np.nan

                                    for note in event.find_all('Note'):

                                        def get_feature_value(f):
                                            if   f == 'mc':
                                                return mc
                                            elif f == 'mn':
                                                return mc_info.mn
                                            elif f == 'staff':
                                                return staff_id
                                            elif f == 'voice':
                                                return voice
                                            elif f == 'onset':
                                                return pointer
                                            elif f == 'duration':
                                                if not grace:
                                                    return duration
                                                else:
                                                    return 0
                                            elif f == 'nominal_duration':
                                                return nominal_duration
                                            elif f == 'gracenote':
                                                return gracenote
                                            elif f == 'scalar':
                                                return dotscalar
                                            elif f == 'tpc':
                                                return int(note.tpc.string) - 14
                                            elif f == 'midi':
                                                return int(note.pitch.string)
                                            elif f == 'volta':
                                                return volta
                                            elif f == 'articulation':
                                                return articulation
                                            elif f == 'tied':
                                                tie = note.find('Spanner', {'type': 'Tie'})
                                                if tie:                                 # -1: end of tie
                                                    t = -1 if tie.find('prev') else 0   #  1: beginning of tie
                                                    t += 1 if tie.find('next') else 0   #  0: both
                                                else:
                                                    t = np.nan
                                                return t

                                        for f in features:
                                            df_vals[f].append(get_feature_value(f))

                                    if not grace:
                                        pointer += duration

                    del nodetypes['voice']

                else:
                    logging.error('Measure without <voice> tag.')

                remaining_tags = [k for k in list(tagtypes) + list(nodetypes.keys()) if not k in TREATED_TAGS]
                if len(remaining_tags) > 0:
                    logging.debug(f"The following tags have not been treated: {remaining_tags}")

        df = pd.DataFrame(df_vals).astype({'volta': 'Int64', 'tied': 'Int64'}, )
        df = df.groupby('mc', group_keys=False).apply(lambda df: df.sort_values(['onset', 'midi']))
        self.notes = df.reset_index(drop=True)

    def __repr__(self):
        return f"{'Repeated s' if self.repeated else 'S'}{'' if self.subsection_of is None else 'ubs'}ection from MC {self.first_mc} ({self.start_break}) to MC {self.last_mc} ({self.end_break}), {'with ' + str(len(self.voltas)) if len(self.voltas) > 0 else 'without'} voltas."

################################################################################
#                             SCORE CLASS
################################################################################
class Score(object):
    """ Parser for MuseScore3 MSCX files.

    NOTE: Measure count ``mc`` refers to the `mc` th measure node, whereas measure
    number ``mn`` refers to the `mn` th measure in the score. The former is the number
    `Bar` displayed in the MuseScore status bar, minus 1 (MS starts counting at 1,
    the parser at 0). The latter can consist of several measure nodes and can be
    split across sections.

    Attributes
    ----------
    dir : :obj:`str`
        Directory where the parsed file is stored.
    file : :obj:`str`
        Absolute or relative path to the MSCX file you want to parse.
    filename : :obj:`str`
        Filename of the parsed file.
    info : obj:`pandas.DataFrame`
        Aggregation and strongly expanded version of the dataframes in `mc_info`.
        Useful for everyday work.
    last_node : :obj:`int`
        Measure count of the score's last measure node.
    mc_info : :obj:`dict` of :obj:`pandas.DataFrame`
        One DataFrame per staff where measure counts are index values and columns
        hold corresponding structural information. This information is best accessed
        in aggregated form in `self.info`.
    measure_nodes : :obj:`dict` of :obj:`dict` of :class:`bs4.element.Tag`
        Keys of the first dict are staff IDs, keys of each inner dict are incremental
        measure counts (NOT measure numbers) and values are XML nodes.
    score : :class:`bs4.BeautifulSoup`
        The complete XML structure of the parsed MSCX file.
    score_features : :obj:`list` of {'articulation'}
        Additional features you want to extract.
    section_order : :obj:`list` of :obj:`int`:
        List of section IDs representing in which order the sections in ``section_structure``
        are presented and repeated.
    section_structure : :obj:`list` of :obj:`tuple` of :obj:`int`
        Keys are section IDs, values are a tuple of two measure counts, the
        (inclusive) boundaries of the section. That is to say, no measure count
        can appear in two different value tuples since every measure can be part
        of only one section.
    sections : :obj:`dict` of :obj:`Section`
        The sections of this score.
    separating_barlines : :obj:`list` of :obj:`str`
        List of barline types that cause the score to be split in separate sections.
        Defaults to `['double']`.
    staff_nodes : :obj:`dict` of :class:`bs4.element.Tag`
        Keys are staff IDs starting with 1, values are XML nodes.
    super_sections : :obj:`dict` of :obj:`list`
        This dictionary has augmenting keys standing for one of the super_sections,
        i.e. sections that are grouped in the score by an englobing repetition,
        represented by lists of section IDs.
    super_section_order : :obj:`list` of :obj:`int`
        A more abstract version of section_order, using the keys from super_sections.
    """

    def __init__(self, file, score_features=[], separating_barlines=['double']):

        # Initialize attributes
        self.file = file
        self.dir, self.filename = os.path.split(os.path.abspath(file))
        self.staff_nodes = {}
        self.measure_nodes = {}
        self.score_features = score_features
        self.sections = {}
        self.section_structure = {}
        self.section_order = []
        self.separating_barlines = separating_barlines
        self.super_sections = {}
        self.super_section_order = []
        self.mc_info = {}
        self.info = pd.DataFrame()

        # Load file
        logging.info(f"Attempting to parse {self.filename}")
        with open(self.file, 'r') as file:
            self.score = bs(file.read(), 'xml')

        # Check Musescore version
        ms_version = self.score.find('programVersion').string
        if ms_version != NEWEST_MUSESCORE:
            logging.warning(f"{self.filename} was created with MuseScore {ms_version}. Auto-conversion will be implemented in the future.")
        assert ms_version.split('.')[0] == '3', f"This is a MS2 file, version {ms_version}"
        # ToDo: Auto-conversion

        #######################################################################
        # Store basic HTML nodes for quick access and extract structural info #
        #######################################################################

        # Extract staves
        for staff in self.score.find('Part').find_next_siblings('Staff'):
            staff_id = int(staff['id'])
            self.staff_nodes[staff_id] = staff
            self.measure_nodes[staff_id] = {}
            logging.debug(f"Stored staff with ID {staff_id}.")

        # Tags to extract from measures and corresponding column to store their values
        # in the df `self.mc_info[staff_id]` after computing them via feature_from_node().
        tag_to_col = {'accidental': 'keysig',
                      'TimeSig': 'timesig',
                      'voice': 'voices',
                      'startRepeat': 'repeats',
                      'endRepeat': 'repeats',
                      'LayoutBreak': 'repeats',
                      'Volta': 'volta',
                      'BarLine': 'barline',
                      'noOffset': 'numbering_offset',
                      'irregular': 'dont_count'
                      }

        cols = ['keysig', 'timesig', 'act_dur', 'voices', 'repeats', 'volta', 'barline', 'numbering_offset', 'dont_count']

        def get_measure_infos(measure):
            """Treat <Measure> node and return info dict."""
            nonlocal new_section
            mc_info = {}
            if new_section:
                mc_info['repeats'] = 'newSection' # if section starts with startRepeat, this is overwritten
                new_section = False
            if measure.has_attr('len'):
                mc_info['act_dur'] = frac(measure['len'])
            infos = defaultdict(list)
            for tag in measure.find_all(tag_to_col.keys()):
                infos[tag.name].append(tag)
            for tag, nodes in infos.items():
                if tag != 'LayoutBreak':
                    col = tag_to_col[tag]
                    mc_info[col] = feature_from_node(tag, nodes)
                else:
                    subtype = nodes[0].find('subtype')
                    if subtype and subtype.string == 'section':
                        new_section = True
            return mc_info


        for staff_id, staff in self.staff_nodes.items():
            mc_info = pd.DataFrame(columns=cols)
            new_section = False    # flag

            for i, measure in enumerate(staff.find_all('Measure')):
                self.measure_nodes[staff_id][i] = measure
                logging.debug(f"Stored the {i}th measure of staff {staff_id}.")

                mc_info = mc_info.append(get_measure_infos(measure), ignore_index=True)
            mc_info.index.name = 'mc'
            self.mc_info[staff_id] = mc_info

        # all staves should have the same number of measures
        mcs = set(len(df.index) for df in self.mc_info.values())
        if len(mcs) > 1:
            logging.error("Staves have different measure counts. Check DataFrames in self.mc_info")

        # Last measure count
        self.last_node =  max(self.mc_info[1].index)

        # Check for infos which are not included in self.mc_info[1]; i.e.,
        # infos appearing only in one of the lower staves.
        for col in self.mc_info[1].columns:
            if not col in ['voices']:    # Exclude columns, that will be aggregated anyway
                cols = [self.mc_info[k][col] for k in self.mc_info.keys()]
                c1 = cols[0]
                cols = cols[1:]
                for i, c in enumerate(cols):
                    if not c1.equals(c):
                        not_in_c1 = c[~nan_eq(c1, c)]
                        if len(not_in_c1.dropna()) > 0:
                            logging.warning(f"These values in mc_info[{i+2}] are not included in mc_info[1]: {not_in_c1}")

        # complete the keysig and timesig infos
        for staff, mc_info in self.mc_info.items():
            first_row = mc_info.iloc[0]
            last_row = mc_info.iloc[-1]
            if isnan(first_row.keysig):
                mc_info.loc[first_row.name,'keysig'] = 0
                logging.debug("Key signature has been set to C major.")
            if isnan(first_row.timesig):
                logging.error(f"Time signature not defined in the first measure of staff {staff}.")
            if not isnan(first_row.repeats):
                logging.warning(f"First measure of staff {staff} has a {first_row.repeats} tag. Information overwritten by 'firstMeasure'")
            mc_info.loc[0, 'repeats'] = 'firstMeasure'
            if isnan(last_row.repeats):
                mc_info.loc[last_row.name, 'repeats'] = 'lastMeasure'
            mc_info[['keysig', 'timesig']] = mc_info[['keysig', 'timesig']].fillna(method='ffill')

        #######################################################################
        # Create the master DataFrame self.info, combining all staves'        #
        # structural information and newly computed infos such as bar numbers #
        #######################################################################

        self.info = self.mc_info[1].copy()
        for df in (self.mc_info[k] for k in self.mc_info.keys() if k > 1):
            self.info.fillna(df, inplace=True)
        if self.info.equals(self.mc_info[1]):
            logging.debug(f"info and mc_info[1] were identical before aggregation.")
        else:
            logging.warning(f"""info and mc_info[1] were not identical before aggregation.
This means that lower staves contain information that's missing in
the first staff (as shown in previous warning).""")
        # complete measure durations
        self.info.insert(2, 'duration', self.info['timesig'].apply(lambda x: frac(x)))
        self.info.act_dur.fillna(self.info.duration, inplace=True)
        # Aggregate values in self.info
        for df in (self.mc_info[k] for k in self.mc_info.keys() if k > 1):
            self.info.voices += df.voices


        ##################################################
        # Calculate and check measure numbers (MN != MC) #
        ##################################################
        # mn are bar numbers as they are shown in MuseScore in the score
        # mc are bar numbers as they are shown in MuseScore in the status bar, minus one
        self.info['mn'] = compute_mn(self.info[['dont_count','numbering_offset']])


        #############################
        # Compute section structure #
        #############################

        # Spreading out volta information
        volta_structure = get_volta_structure(self.info)
        for group in volta_structure:
            for i, mc in enumerate(group):
                self.info.loc[mc, 'volta'] = i+1


        def create_section(fro, to, repeated=False):
            """
            Parameters
            ----------
            fro, to : `int`
                First and last measure count of the new section.
            repeated : bool, opt
                Whether or not the new section is repeated.
            """
            nonlocal section_counter, super_counter
            start_reason = 'start' if isnan(self.info.repeats[fro]) else self.info.repeats[fro]
            end_reason   = 'end'   if isnan(self.info.repeats[to])  else self.info.repeats[to]
            if start_reason == 'start':
                start_reason = 'startRepeat' if repeated else 'startNormal'
            if end_reason == 'end':
                end_reason = 'endRepeat' if repeated else 'endNormal'

            inner_structure = self.info.loc[fro+1:to-1]   # measure infos excluding starting and ending measure
            # check whether this section contains separating barlines: then, subsections have to be created
            splits = inner_structure.barline.isin(self.separating_barlines)
            if splits.any():
                subsections = []
                boundaries = inner_structure.barline[splits].apply(lambda x: x + '_barline')
                split_mcs = boundaries.index.to_list()
                bounds = sorted([fro, to] + split_mcs + [i+1 for i in split_mcs])
                reasons = [start_reason] + [reason for reason in boundaries.to_list() for _ in (0,1)] + [end_reason]
                if len(reasons) != len(bounds):
                    logging.critical("Implementation error.")
                for i in range(len(bounds)//2):
                    f, t = bounds[2*i], bounds[2*i+1]   # "from mc" and "to mc" for subsections
                    f_reason, t_reason = reasons[2*i], reasons[2*i+1]
                    self.section_structure[section_counter] = (f, t)
                    self.sections[section_counter] = Section(self, f, t, section_counter, repeated, f_reason, t_reason)
                    subsections.append(section_counter)
                    section_counter += 1
            else:
                self.section_structure[section_counter] = (fro, to)
                self.sections[section_counter] = Section(self, fro, to, section_counter, repeated, start_reason, end_reason)
                subsections = [section_counter]
                section_counter += 1

            self.section_order.extend(subsections * (repeated + 1))
            self.super_sections[super_counter] = subsections
            if len(subsections) > 1:
                for s in subsections:
                    self.sections[s].subsection_of = super_counter
            self.super_section_order.extend([super_counter] * (repeated + 1))
            super_counter += 1
            logging.debug(f"Created {'repeated ' if repeated else ''}section from {fro} to {to}.")
            nonlocal last_to
            last_to = to
            ########################## end of create_section()

        # Compute (from_mc, to_mc) tuples of all repeated sections and create sections
        repeat_structure = compute_repeat_structure(self.info)
        last_to = -1
        section_counter, super_counter = 0, 0
        to = 0    # In case there are no repeats
        for fro, to in repeat_structure:
            if fro != last_to + 1:
                create_section(last_to+1, fro-1)    # create unrepeated section
            create_section(fro, to, True)           # create repeated   section
        if to != self.last_node:
            create_section(to+1 if to > 0 else 0, self.last_node)

        # Add volta groups to section objects
        sections = (t for t in self.section_structure.items())
        section, (fro, to) = next(sections)
        for group in volta_structure:
            volta_mcs = sum(group, [])
            while any(True for mc in volta_mcs if mc > to):
                section, (fro, to) = next(sections)
            self.sections[section].voltas = group

        # Add sections to info frame
        self.info.insert(0, 'section', pd.Series(np.nan, dtype='Int64'))
        for s, (fro, to) in self.section_structure.items():
            self.info.loc[fro:to, 'section'] = s

        if self.info.section.isna().any():
            logging.critical("Not all measure nodes have been assigned to a section.")

        # check that no note crosses measure boundary
        check_measure_boundaries(self.get_notes(), self.info.act_dur)

        # store first_mn and last_mn for all sections
        for k, v in self.sections.items():
            mns = self.info.mn[self.info.section == k]
            v.first_mn = mns.iloc[0]
            v.last_mn = mns.iloc[-1]


        # Compute the subsequent mc for every mc
        ix = self.info.index
        self.info['next'] = pd.Series(len(ix)*[[]], index=ix, dtype=object)
        mcs = self.info.reset_index()['mc']
        before_volta = {}
        for section in self.sections.values():
            fro, to = section.first_mc, section.last_mc
            volta_mcs = sum(section.voltas, [])
            repeat_slice = None
            if len(volta_mcs) == 0:                     # if this section has no voltas
                normal_slice = list(range(fro, to+1))   # all measures are followed by +1 ("normal")
                if section.repeated:                    # and when repeated, the last one
                    repeat_slice = [to]                 # will also be followed by the section's beginning
            else:
                normal_slice = [i for i in range(fro, to+1) if not i in volta_mcs]
                n_voltas = len(section.voltas)
                for i, group in enumerate(reversed(section.voltas)):        # iterate backwards through voltas
                    if i < n_voltas - 1:                                    # check for all voltas but the first
                        group_info = self.info.loc[group]             # whether they are exluded from bar count
                        wrongly_counted = group_info.dont_count.isna() & group_info.numbering_offset.isna()
                        if wrongly_counted.any():
                            not_ex = mcs[group][wrongly_counted].values
                            plural = 1 if len(not_ex) > 1 else 0
                            logging.warning(f"MC{'s' if plural else ''} {not_ex if plural else not_ex[0]} in volta {group} {'have' if plural else 'has'} not been excluded from barcount.")
                    if i == 0:                                              # last volta:
                        normal_slice.extend(group)                          # just normal
                        # check
                        repeat_vals = self.info.repeats.loc[group].values
                        if any(rep in repeat_vals for rep in ['startRepeat', 'endRepeat']):
                            logging.warning(f"Final volta with MC {group} contains a repeat sign.")
                    else:                                                   # previous voltas:
                        for j, mc in enumerate(reversed(group)):            # iterate backwards through measure counts
                            if j == 0:                                      # the last one goes back to section's beginning
                                # check
                                self.info.loc[mc, 'next'] = [[fro]]
                                if self.info.loc[mc, 'repeats'] != 'endRepeat':
                                    logging.warning(f"Volta with MC {group} is missing the endRepeat.")
                            else:                                           # previous MCs just normal
                                normal_slice.append(mc)
                try:
                    before_volta[mc-1] = [group[0] for group in section.voltas] # store measure before the first volta and a list holding
                                                                                # the first measure of each volta which can follow it
                except:
                    print(section.voltas, mc)
        # Fill the column 'next'
            self.info.loc[normal_slice, 'next'] = mcs.loc[normal_slice].apply(lambda x: [x+1])
            if repeat_slice:
                self.info.loc[repeat_slice, 'next'] = self.info.loc[repeat_slice, 'next'].apply(lambda x: x + [fro])
        self.info.loc[before_volta.keys(), 'next'] = pd.Series(before_volta)
        self.info.iloc[-1:, self.info.columns.get_loc('next')].apply(lambda x: x.remove(self.last_node+1))

        # Calculate offsets for split measures and check for correct measure numbering
        not_excluded = lambda r: isnan(r.dont_count) and isnan(r.numbering_offset)
        measures_to_check = (self.info.act_dur != self.info.duration) | (self.info.repeats == 'endRepeat')
        check = self.info[measures_to_check]
        self.info.insert(5, 'offset', 0)
        for ix, r in check.iterrows():
            if r.act_dur > r.duration:
                logging.info(f"MC {ix} is longer than its nominal value.")
            elif r.act_dur == r.duration:           # endRepeat
                next_mcs = self.info.loc[r.next]
                irregular = next_mcs.act_dur != next_mcs.duration
                if irregular.any():
                    irr_mcs = next_mcs[irregular].index.to_list()
                    irr_vals = ', '.join([str(fr) for fr in next_mcs[irregular].act_dur.values])
                    plural = 1 if len(irr_mcs) > 1 else 0
                    logging.warning(f"The endRepeat in MC {ix} ({r.act_dur}) is not adapted to the irregular measure length{'s' if plural else ''} in MC{'s' if plural else ''} {irr_mcs if plural else irr_mcs[0]} ({irr_vals})")
            elif ix == 0:                           # anacrusis
                self.info.loc[ix, 'offset'] = r.duration - r.act_dur
                if not_excluded(r):
                    logging.warning(f"MC {ix} seems to be a pickup measure but has not been excluded from bar count!")
            else:                                   # incomplete measure
                if self.info.loc[ix, 'offset'] == 0:                            # beginning of an incomplete measure
                    missing = r.duration - r.act_dur
                    if not isnan(r.next):
                        for n in r.next:
                            if self.info.loc[n].act_dur == missing:
                                self.info.loc[n, 'offset'] = r.act_dur
                                if not_excluded(self.info.loc[n]):
                                    logging.warning(f"MC {n} is completing MC {ix} but has not been excluded from bar count!")
                            else:
                                logging.warning(f"MC {ix} ({r.act_dur}) and MC {n} ({self.info.loc[n].act_dur}) don't add up to {r.duration}.")
        logging.info(f"Done parsing {self.filename}")



    def get_notes(self, section=None, multiindex=True, beatsize=None, **kwargs):
        """ Retrieve list of notes as a DataFrame.

        Parameters
        ----------
        section : (`collection` of) :obj:`int`
            Sections of which you want to see the notes. 0 = first, -1 = last
            (a, b) = sections from a to b; [a, b] = sections a and b
            Defaults to None which shows all sections. Repeating values in a collection
            leads to the corresponding note lists being repeated.
        multiindex : :obj:`bool`, optional
            Is True by default, in which case section numbers are displayed as the first level of a
            MultiIndex and a sequential index as the second. Pass `multiindex=False` to yield a note list
            with a single RangeIndex.
        beatsize : :obj:`bool` or :obj:`dict` or `fraction`, optional
            Adds a column with a beat for every note.
            If True, the dict TIMESIG_BEAT is used to determine the beat size according to the time signature.
            By passing a dictionary you can overwrite or enhance the information in TIMESIG_BEAT.
            If you pass a fraction (such as '1/4', 1/4, frac(1/4) or 0.25), this beat size is used for all time signatures.
        **kwargs
            The following columns can be added by setting them to True:
            * octaves
            * note_names
            * beats
            * pcs (pitch classes 0-11)
            These and all other columns of the notelist can be filtered by passing feature=selector.
            The features are {'n', 'mc', 'mn', 'onset', 'duration', 'gracenote',
            'nominal_duration', 'scalar', 'tied', 'tpc', 'midi', 'staff', 'voice',
            'volta', 'octaves', 'note_names', 'beats'}
            The selectors can be:
            * Single value: Only notes where `feature` equals `selector`.
            * 2-tuple (a,b): Slice where `a <= feature <= b`.
            * Collection: All notes where `feature` is in `selector`.
            * True yields all values that are not numpy.nan

        Examples
        --------
        S.get_notes()                           # all notes
        S.get_notes(0)                          # notes from the first section only
        S.get_notes(-1)                         # notes from the last section only
        S.get_notes((0,3))                      # sections [0,1,2,3]
        S.get_notes((3,0))                      # section [3,2,1,0]
        S.get_notes((-4,-1))                    # fourth last to last section
        S.get_notes(None, False)                # all notes with single RangeIndex
        S.get_notes(beatsize=True)              # added column with beat sizes according to time signature
        S.get_notes(beatsize=1/4)               # added column with every note's quarter beat
        S.get_notes(note_names=True)            # added column with note names
        S.get_notes(note_names=['A', 'D', 'G']) # Only these notenames
        S.get_notes(octaves=True)               # added column with notes' octaves
        S.get_notes(octaves=(6,10))             # Only notes in octaves 6 through 10
        S.get_notes(pcs=True)                   # added column with (MIDI) pitch classes
        S.get_notes(pcs=[1,3,6,8,10])           # only black keys on the piano
        S.get_notes(mn=1)                       # all notes with measure number 1
        S.get_notes(duration=[0.5, 1])          # all notes with duration of a half or a whole note
        S.get_notes(duration=(0.5, 1))          # all notes with duration of a half, a whole, or in between
        S.get_notes(n=(0,5))                    # get first 5 notes from every section
        """


        # Get note lists for requested sections and save as df
        n = len(self.sections)
        if section is None:
            section = self.sections.keys()
            df = pd.concat([self.sections[s].notes for s in section], keys=section, names=['section', 'ix'])
        elif section.__class__ == int:
            s = treat_section_index(section, n)
            if s is None:
                raise ValueError(f"Section {section} does not exist.")
            df = pd.concat([self.sections[s].notes], keys=[s], names=['section', 'ix'])
        else:
            treated = treat_section_index(section, n)
            if section.__class__ == tuple and len(section) == 2:
                fro, to = treated[0], treated[1]
                if fro is None:
                    fro = 0
                    logging.warning(f"Replaced {section[0]} in {section} by first section 0.")
                if to is None:
                    to = n-1
                    logging.warning(f"Replaced {section[1]} in {section} by last section {to}.")
                if to >= fro:
                    treated = list(range(fro, to+1))
                else:
                    treated = list(reversed(range(to, fro+1)))
                treated = treat_section_index(treated, n)
            treated = [t for t in treated if t is not None]
            if len(treated) > 0:
                c = Counter(treated)
                multiples = {k: v for k, v in c.items() if v > 1}
                if len(multiples) == 0:
                    df = pd.concat([self.sections[s].notes for s in treated], keys=treated, names=['section', 'ix'])
                else:
                    new_keys = {k: a_n_range('a',v) for k,v in multiples.items()}
                    keys = [f"{s}{next(new_keys[s])}" if s in new_keys else s for s in treated]
                    df = pd.concat([self.sections[s].notes for s in treated], keys=keys, names=['section', 'ix'])
            else:
                return None


        # activate requested features
        available_features = ['octaves', 'note_names', 'beats', 'pcs', 'n']
        features = {f: False for f in available_features}
        if beatsize is not None:
            features['beats'] = True
        for feature, val in kwargs.items():
            if not feature in df.columns:
                if not feature in features.keys():
                    logging.warning(f"{feature} is not part of the note features.")
                features[feature] = True

        # compute additional feature columns
        if features['octaves']:
            df['octaves'] = midi2octave(df.midi)
        if features['note_names']:
            df['note_names'] = spell_tpc(df.tpc)
        if features['beats']:
            if beatsize is None or beatsize.__class__ == bool:
                beatsize = {}
            if beatsize.__class__ == dict:
                beatsizedict = defaultdict(lambda: frac(1/4))
                beatsizedict.update(TIMESIG_BEAT)
                beatsizedict.update(beatsize)
                for k in beatsizedict.keys():
                    beatsizedict[k] = frac(beatsizedict[k])
            elif beatsize.__class__ == frac:
                beatsizedict = defaultdict(lambda: beatsize)
            else:
                try:
                    val = frac(beatsize)
                    beatsizedict = defaultdict(lambda: val)
                except:
                    raise ValueError(f"Datatype of beats = {beatsize} not understood")

            def compute_beat(r):
                size = beatsizedict[r.timesig]
                onset = r.onset + r.offset
                beat = onset // size + 1
                subbeat = (onset % size) / size
                if subbeat > 0:
                    return f"{beat}.{subbeat}"
                else:
                    return str(beat)

            df['beats'] = self.info[['timesig','offset']]\
                              .merge(df[['mc', 'onset']], on='mc', left_index=True)\
                              .apply(compute_beat, axis=1)
        if features['pcs']:
            df['pcs'] = df.midi % 12
        if features['n']:
            df = df.reset_index(1, drop=False)\
                              .set_index('ix',drop=False, append=True)\
                              .rename(columns={'ix': 'n'})

        # apply requested filters
        for feature, selector in kwargs.items():
            if selector.__class__ == int or selector.__class__ == str:
                sel = df[feature] == selector
            elif selector.__class__ == bool:
                if selector:
                    if feature == 'tied':
                        sel = df.tied.isin([0,1])
                    else:
                        sel = df[feature].notna()
                else:
                    continue
            elif selector.__class__ == tuple and len(selector) == 2:
                sel = (selector[0] <= df[feature]) & (df[feature] <= selector[1])
            else:
                try:
                    if len(selector) == 1:
                        sel = df[feature] == selector
                    else:
                        sel = df[feature].isin(selector)
                except:
                    sel = df[feature] == selector
            try:
                df = df[sel]
            except:
                raise ValueError(f"Error while applying filter to {feature}: df[{sel}] caused an error.")

        # Postprocessing
        if len(df) == 0:
            logging.info(f"No notes exist for this selection.")
        if 'n' in df.columns:
            df.drop(columns='n', inplace=True)
        if not multiindex:
            df.reset_index(drop=True, inplace=True)

        return df


# %% Playground
#for subdir, dirs, files in os.walk('scores'):
#    for file in files:
#        if file.endswith('mscx'):
#            S = Score(os.path.join(subdir,file))
# S = Score('./scores/366/D366ländler11.mscx')
# S.section_structure
# compute_repeat_structure(S.info)
# S.sections[0]
#S.get_notes((12,6))
# S.get_notes(1, True, True)
# S.sections[1].events[S.sections[1].events.mc == 11]
# S.sections
# S.section_structure
# S.section_order
# S.super_sections
# S.super_section_order
# logging.basicConfig(level=logging.DEBUG)
# logging.getLogger().setLevel(logging.DEBUG)
#logging.getLogger().setLevel(logging.INFO)
# %% Exclude this from the main cell

################################################################################
#                           COMMANDLINE USAGE
################################################################################
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description = '''\
-------------------------------------
| Parser for MuseScore3 MSCX files. |
-------------------------------------

At the moment, this is just a skeleton. Later, the commandline can be used to
quickly parse entire folders and store files with the computed data.''')
    parser.add_argument('file',metavar='FILE',help='Absolute or relative path to the MSCX file you want to parse.')
    parser.add_argument('-l','--logging',default='INFO',help="Set logging to one of the levels {DEBUG, INFO, WARNING, ERROR, CRITICAL}.")
    args = parser.parse_args()

    logging_levels = {
        'DEBUG':    logging.DEBUG,
        'INFO':     logging.INFO,
        'WARNING':  logging.WARNING,
        'ERROR':    logging.ERROR,
        'CRITICAL':  logging.CRITICAL
        }
    logging.basicConfig(level=logging_levels[args.logging])
    S = score(args.file)
    print("Successfully parsed.")
