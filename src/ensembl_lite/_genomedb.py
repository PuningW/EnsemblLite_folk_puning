import collections
import dataclasses
import functools
import itertools
import pathlib
import re
import sqlite3
import typing

from abc import ABC, abstractmethod
from typing import Any, Optional

import click
import h5py
import numpy
import typing_extensions

from cogent3 import get_moltype, make_seq, make_table, open_
from cogent3.app.composable import define_app
from cogent3.core.annotation import Feature
from cogent3.core.annotation_db import (
    FeatureDataType,
    OptionalInt,
    OptionalStr,
    _select_records_sql,
)
from cogent3.core.sequence import Sequence
from cogent3.parse.fasta import MinimalFastaParser
from cogent3.parse.gff import GffRecord, gff_parser, is_gff3
from cogent3.util.io import iter_splitlines
from cogent3.util.table import Table
from numpy.typing import NDArray

from ensembl_lite._config import Config, InstalledConfig
from ensembl_lite._db_base import Hdf5Mixin, SqliteDbMixin
from ensembl_lite._species import Species
from ensembl_lite._util import _HDF5_BLOSC2_KWARGS, PathType


_SEQDB_NAME = "genome_sequence.hdf5_blosc2"
_ANNOTDB_NAME = "features.ensembl_gff3db"

_typed_id = re.compile(
    r"\b[a-z]+:", flags=re.IGNORECASE
)  # ensembl stableid's prefixed by the type
_feature_id = re.compile(r"(?<=\bID=)[^;]+")
_exon_id = re.compile(r"(?<=\bexon_id=)[^;]+")
_parent_id = re.compile(r"(?<=\bParent=)[^;]+")


def _lower_case_match(match) -> str:
    return match.group(0).lower()


def tidy_gff3_stableids(attrs: str) -> str:
    """makes the feature type prefix lowercase in gff3 attribute fields"""
    return _typed_id.sub(_lower_case_match, attrs)


class EnsemblGffRecord(GffRecord):
    __slots__ = GffRecord.__slots__ + ("feature_id",)

    def __init__(self, feature_id: Optional[int] = None, **kwargs):
        is_canonical = kwargs.pop("is_canonical", None)
        super().__init__(**kwargs)
        self.feature_id = feature_id
        if is_canonical:
            self.attrs = "Ensembl_canonical;" + self.attrs or ""

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other):
        return self.name == getattr(other, "name", other)

    @property
    def stableid(self):
        return _typed_id.sub("", self.name or "")

    @property
    def is_canonical(self):
        attrs = self.attrs or ""
        return "Ensembl_canonical" in attrs

    def update_from_attrs(self) -> None:
        """updates attributes from the attrs string

        Notes
        -----
        also updates biotype from the prefix in the name
        """
        attrs = self.attrs
        id_regex = _feature_id if "ID=" in attrs else _exon_id
        attr = tidy_gff3_stableids(attrs)
        if feature_id := id_regex.search(attr):
            self.name = feature_id.group()

        if pid := _parent_id.search(attr):
            parents = pid.group().split(",")
            # now sure how to handle multiple-parent features
            # so taking first ID as the parent for now
            self.parent_id = parents[0]

        if ":" in (self.name or ""):
            biotype = self.name.split(":")[0]
            self.biotype = "mrna" if biotype == "transcript" else biotype

    @property
    def size(self) -> int:
        """the sum of span segments"""
        return 0 if self.spans is None else sum(abs(s - e) for s, e in self.spans)


def custom_gff_parser(
    path: PathType, num_fake_ids: int
) -> tuple[dict[str, EnsemblGffRecord], int]:
    """replacement for cogent3 merged_gff_records"""
    reduced = {}
    gff3 = is_gff3(path)
    for record in gff_parser(
        iter_splitlines(path),
        gff3=gff3,
        make_record=EnsemblGffRecord,
    ):
        record.update_from_attrs()
        if not record.name:
            record.name = f"unknown-{num_fake_ids}"
            num_fake_ids += 1

        if record.name not in reduced:
            record.spans = record.spans or []
            reduced[record] = record

        reduced[record].spans.append([record.start, record.stop])
        reduced[record].start = min(reduced[record].start, record.start)
        reduced[record].stop = max(reduced[record].stop, record.stop)

    return reduced, num_fake_ids


DbTypes = typing.Union[sqlite3.Connection, "EnsemblGffDb"]


class EnsemblGffDb(SqliteDbMixin):
    _biotype_schema = {
        "type": "TEXT COLLATE NOCASE",
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    }
    _feature_schema = {
        "seqid": "TEXT COLLATE NOCASE",
        "source": "TEXT COLLATE NOCASE",
        "biotype_id": "TEXT",
        "start": "INTEGER",
        "stop": "INTEGER",
        "score": "TEXT",  # check defn
        "strand": "TEXT",
        "phase": "TEXT",
        "attributes": "TEXT",
        "comments": "TEXT",
        "spans": "array",  # aggregation of coords across records
        "stableid": "TEXT",
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "is_canonical": "INTEGER",
    }
    # relationships are directional, but can span levels, eg.
    # gene -> transcript -> CDS / Exon
    # gene -> CDS
    _related_feature_schema = {"gene_id": "INTEGER", "related_id": "INTEGER"}

    _index_columns = {
        "feature": (
            "seqid",
            "stableid",
            "start",
            "stop",
            "is_canonical",
            "biotype_id",
        ),
        "related_feature": ("gene_id", "related_id"),
    }

    def __init__(
        self,
        source: PathType = ":memory:",
        db: typing.Optional[DbTypes] = None,
    ):
        self.source = source
        if isinstance(db, self.__class__):
            db = db.db

        self._db = db
        self._init_tables()
        self._create_views()

    def __hash__(self):
        return id(self)

    def __eq__(self, other) -> bool:
        return id(self) == id(other)

    def _create_views(self) -> None:
        """define views to simplify queries"""
        sql = """
        CREATE VIEW IF NOT EXISTS gff AS
        SELECT f.seqid as seqid,
               b.type as biotype,
               f.start as start,
               f.stop as stop,
               f.strand as strand,
               f.spans as spans,
               f.stableid as name,
               f.is_canonical as is_canonical,
               f.id as feature_id
        FROM feature f
        JOIN biotype b ON f.biotype_id = b.id
        """
        self._execute_sql(sql)
        # view to query for child given parent id and vice versa
        p2c = """
        CREATE VIEW IF NOT EXISTS parent_to_child AS
        SELECT fc.stableid as name,
               fp.stableid as parent_stableid,
               fc.seqid as seqid,
               b.type as biotype,
               fc.start as start,
               fc.stop as stop,
               fc.strand as strand,
               fc.spans as spans,
               fc.is_canonical as is_canonical
        FROM related_feature r 
        JOIN biotype b ON fc.biotype_id = b.id
        JOIN feature fp ON fp.id = r.gene_id
        JOIN feature fc ON fc.id = r.related_id
        """
        self._execute_sql(p2c)
        c2p = """
        CREATE VIEW IF NOT EXISTS child_to_parent AS
        SELECT fp.stableid as name,
               fc.stableid as child_stableid,
               fp.seqid as seqid,
               b.type as biotype,
               fp.start as start,
               fp.stop as stop,
               fp.strand as strand,
               fp.is_canonical as is_canonical,
               fp.spans as spans
        FROM related_feature r 
        JOIN biotype b ON fp.biotype_id = b.id
        JOIN feature fp ON fp.id = r.gene_id
        JOIN feature fc ON fc.id = r.related_id
        """
        self._execute_sql(c2p)

    def __len__(self) -> int:
        return self.num_records()

    @functools.cache
    def _get_biotype_id(self, biotype: str) -> int:
        sql = "INSERT OR IGNORE INTO biotype(type) VALUES (?) RETURNING id"
        result = self.db.execute(sql, (biotype,)).fetchone()
        return result["id"]

    def _build_feature(self, kwargs) -> EnsemblGffRecord:
        # not supporting this at present, which comes from cogent3
        # alignment objects
        kwargs.pop("on_alignment", None)
        return EnsemblGffRecord(**kwargs)

    def add_feature(
        self, *, feature: typing.Optional[EnsemblGffRecord] = None, **kwargs
    ) -> None:
        """updates the feature_id attribute"""
        if feature is None:
            feature = self._build_feature(kwargs)

        id_cols = ("biotype_id", "id")
        cols = [col for col in self._feature_schema if col not in id_cols]
        # do conversion to numpy array after the above statement to avoid issue of
        # having a numpy array in a conditional
        feature.spans = numpy.array(feature.spans)
        feature.start = feature.start or int(feature.spans.min())
        feature.stop = feature.stop or int(feature.spans.max())
        vals = [feature[col] for col in cols] + [self._get_biotype_id(feature.biotype)]
        cols += ["biotype_id"]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO feature({','.join(cols)}) VALUES ({placeholders}) RETURNING id"
        result = self.db.execute(sql, tuple(vals)).fetchone()
        feature.feature_id = result["id"]

    def add_records(
        self,
        *,
        records: typing.Iterable[EnsemblGffRecord],
        gene_relations: dict[EnsemblGffRecord, set[EnsemblGffRecord]],
    ) -> None:
        for record in records:
            self.add_feature(feature=record)

        # now add the relationships
        sql = "INSERT INTO related_feature(gene_id, related_id) VALUES (?,?)"
        for gene, children in gene_relations.items():
            if gene.feature_id is None:
                raise ValueError(f"gene.feature_id not defined for {gene!r}")

            child_ids = [child.feature_id for child in children]
            if None in child_ids:
                raise ValueError(f"child.feature_id not defined for {children!r}")

            comb = [tuple(c) for c in itertools.product([gene.feature_id], child_ids)]
            self.db.executemany(sql, comb)

    def num_records(self) -> int:
        return self._execute_sql("SELECT COUNT(*) as count FROM feature").fetchone()[
            "count"
        ]

    def _get_records_matching(
        self, table_name: str, **kwargs
    ) -> typing.Iterator[sqlite3.Row]:
        """return all fields"""
        columns = kwargs.pop("columns", None)
        allow_partial = kwargs.pop("allow_partial", False)
        # now
        sql, vals = _select_records_sql(
            table_name=table_name,
            conditions=kwargs,
            columns=columns,
            allow_partial=allow_partial,
        )
        yield from self._execute_sql(sql, values=vals)

    def get_features_matching(
        self,
        *,
        seqid: OptionalStr = None,
        biotype: OptionalStr = None,
        name: OptionalStr = None,
        start: OptionalInt = None,
        stop: OptionalInt = None,
        strand: OptionalStr = None,
        attributes: OptionalStr = None,
        allow_partial: bool = False,
        **kwargs,
    ) -> typing.Iterator[FeatureDataType]:
        kwargs = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "kwargs") and v is not None
        }
        # alignment features are created by the user specific
        columns = ("seqid", "biotype", "spans", "strand", "name")
        query_args = {**kwargs}

        for result in self._get_records_matching(
            table_name="gff", columns=columns, **query_args
        ):
            result = dict(zip(columns, result))
            result["spans"] = [tuple(c) for c in result["spans"]]
            yield result

    def get_feature_children(
        self,
        *,
        name: str,
        **kwargs,
    ) -> typing.List[FeatureDataType]:
        cols = "seqid", "biotype", "spans", "strand", "name"
        results = {}
        for result in self._get_records_matching(
            table_name="parent_to_child", columns=cols, parent_stableid=name, **kwargs
        ):
            result = dict(zip(cols, result))
            result["spans"] = [tuple(c) for c in result["spans"]]
            results[result["name"]] = result
        return list(results.values())

    def get_feature_parent(
        self,
        *,
        name: str,
        **kwargs,
    ) -> typing.List[FeatureDataType]:
        cols = "seqid", "biotype", "spans", "strand", "name"
        results = {}
        for result in self._get_records_matching(
            table_name="child_to_parent", columns=cols, child_stableid=name
        ):
            result = dict(zip(cols, result))
            results[result["name"]] = result
        return list(results.values())

    def get_records_matching(
        self,
        *,
        biotype: OptionalStr = None,
        seqid: OptionalStr = None,
        name: OptionalStr = None,
        start: OptionalInt = None,
        stop: OptionalInt = None,
        strand: OptionalStr = None,
        attributes: OptionalStr = None,
        allow_partial: bool = False,
    ) -> typing.Iterator[FeatureDataType]:
        kwargs = {
            k: v for k, v in locals().items() if k not in ("self", "allow_partial")
        }
        sql, vals = _select_records_sql("gff", kwargs, allow_partial=allow_partial)
        col_names = None
        for result in self._execute_sql(sql, values=vals):
            if col_names is None:
                col_names = result.keys()
            yield {c: result[c] for c in col_names}

    def biotype_counts(self) -> dict[str, int]:
        sql = "SELECT biotype, COUNT(*) as count FROM gff GROUP BY biotype"
        result = self._execute_sql(sql).fetchall()
        return {r["biotype"]: r["count"] for r in result}

    def subset(
        self,
        *,
        source: PathType = ":memory:",
        biotype: OptionalStr = None,
        seqid: OptionalStr = None,
        name: OptionalStr = None,
        start: OptionalInt = None,
        stop: OptionalInt = None,
        strand: OptionalStr = None,
        attributes: OptionalStr = None,
        allow_partial: bool = False,
    ) -> typing_extensions.Self:
        """returns a new db instance with records matching the provided conditions"""
        # make sure python, not numpy, integers
        start = start if start is None else int(start)
        stop = stop if stop is None else int(stop)

        kwargs = {k: v for k, v in locals().items() if k not in {"self", "source"}}

        newdb = self.__class__(source=source)
        if not len(self):
            return newdb

        # we need to recreate the values that get passed to add_records
        # so first identify the feature IDs that match the criteria
        cols = None

        feature_ids = {}
        for r in self._get_records_matching(table_name="gff", **kwargs):
            if cols is None:
                cols = r.keys()
            r = dict(zip(cols, r))
            feature_id = r.pop("feature_id")
            feature = EnsemblGffRecord(**r)
            feature_ids[feature_id] = feature

        # now build the related features by selecting the rows with matches
        # in both columns to feature_ids
        ids = ",".join(str(i) for i in feature_ids)
        sql = f"""
        SELECT gene_id, related_id FROM related_feature 
        WHERE gene_id IN ({ids}) AND related_id IN ({ids})
        """
        related = collections.defaultdict(set)
        for record in self._execute_sql(sql):
            gene_id, related_id = record["gene_id"], record["related_id"]
            gene = feature_ids[gene_id]
            related[gene].add(feature_ids[related_id])

        newdb.add_records(records=feature_ids.values(), gene_relations=related)
        return newdb


def make_gene_relationships(
    records: typing.Sequence[EnsemblGffRecord],
) -> dict[EnsemblGffRecord, set[EnsemblGffRecord]]:
    """returns all feature children of genes"""
    related = {}
    for record in records:
        biotype = related.get(record.biotype.lower(), {})
        biotype[record.name] = record
        related[record.biotype.lower()] = biotype

    # reduce the related dict into gene_id by child/grandchild ID
    genes = {}
    for cds_record in related["cds"].values():
        mrna_record = related["mrna"][cds_record.parent_id]
        if mrna_record.is_canonical:
            # we make the CDS identifiable as being canonical
            # this token is used by is_canonical property
            cds_record.attrs = f"Ensembl_canonical;{cds_record.attrs}"

        gene = related["gene"][mrna_record.parent_id]
        gene_relationships = genes.get(gene.stableid, set())
        gene_relationships.update((cds_record, mrna_record))
        genes[gene] = gene_relationships

    return genes


def make_annotation_db(src_dest: tuple[PathType, PathType]) -> bool:
    """convert gff3 file into a EnsemblGffDb

    Parameters
    ----------
    src_dest
        path to gff3 file, path to write AnnotationDb
    """
    src, dest = src_dest
    if dest.exists():
        return True

    db = EnsemblGffDb(source=dest)
    records, _ = custom_gff_parser(src, 0)
    related = make_gene_relationships(records)
    db.add_records(records=records.values(), gene_relations=related)
    db.make_indexes()
    db.close()
    del db
    return True


def _rename(label: str) -> str:
    return label.split()[0]


@define_app
class fasta_to_hdf5:
    def __init__(self, config: Config, label_to_name=_rename):
        self.config = config
        self.label_to_name = label_to_name

    def main(self, db_name: str) -> bool:
        src_dir = self.config.staging_genomes / db_name
        dest_dir = self.config.install_genomes / db_name

        seq_store = SeqsDataHdf5(
            source=dest_dir / _SEQDB_NAME,
            species=Species.get_species_name(db_name),
            mode="w",
        )

        src_dir = src_dir / "fasta"
        for path in src_dir.glob("*.fa.gz"):
            for label, seq in MinimalFastaParser(iter_splitlines(path)):
                seqid = self.label_to_name(label)
                seq_store.add_record(seqid=seqid, seq=seq)
                del seq

        seq_store.close()

        return True


def _get_seqs(src: PathType) -> list[tuple[str, str]]:
    with open_(src) as infile:
        data = infile.read().splitlines()
    name_seqs = list(MinimalFastaParser(data))
    return [(_rename(name), seq) for name, seq in name_seqs]


T = tuple[PathType, list[tuple[str, str]]]


class SeqsDataABC(ABC):
    """interface for genome sequence storage"""

    # the storage reference, e.g. path to file
    source: PathType
    species: str
    mode: str  # as per standard file opening modes, r, w, a
    _is_open = False
    _file: Optional[Any] = None

    @abstractmethod
    def __hash__(self): ...

    @abstractmethod
    def add_record(self, *, seqid: str, seq: str): ...

    @abstractmethod
    def add_records(self, *, records: typing.Iterable[list[str, str]]): ...

    @abstractmethod
    def get_seq_str(
        self, *, seqid: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> str: ...

    @abstractmethod
    def get_seq_arr(
        self, *, seqid: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> NDArray[numpy.uint8]: ...

    @abstractmethod
    def get_coord_names(self) -> tuple[str]: ...

    @abstractmethod
    def close(self): ...


@define_app
class str2arr:
    """convert string to array of uint8"""

    def __init__(self, moltype: str = "dna", max_length=None):
        moltype = get_moltype(moltype)
        self.canonical = "".join(moltype)
        self.max_length = max_length
        extended = "".join(list(moltype.alphabets.degen))
        self.translation = b"".maketrans(
            extended.encode("utf8"),
            "".join(chr(i) for i in range(len(extended))).encode("utf8"),
        )

    def main(self, data: str) -> numpy.ndarray:
        if self.max_length:
            data = data[: self.max_length]

        b = data.encode("utf8").translate(self.translation)
        return numpy.array(memoryview(b), dtype=numpy.uint8)


@define_app
class arr2str:
    """convert array of uint8 to str"""

    def __init__(self, moltype: str = "dna", max_length=None):
        moltype = get_moltype(moltype)
        self.canonical = "".join(moltype)
        self.max_length = max_length
        extended = "".join(list(moltype.alphabets.degen))
        self.translation = b"".maketrans(
            "".join(chr(i) for i in range(len(extended))).encode("utf8"),
            extended.encode("utf8"),
        )

    def main(self, data: numpy.ndarray) -> str:
        if self.max_length:
            data = data[: self.max_length]

        b = data.tobytes().translate(self.translation)
        return bytearray(b).decode("utf8")


@dataclasses.dataclass
class SeqsDataHdf5(Hdf5Mixin, SeqsDataABC):
    """HDF5 sequence data storage"""

    def __init__(
        self,
        source: PathType,
        species: Optional[str] = None,
        mode: str = "r",
        in_memory: bool = False,
    ):
        # note that species are converted into the Ensembl db prefix

        source = pathlib.Path(source)
        self.source = source

        if mode == "r" and not source.exists():
            raise OSError(f"{self.source!s} not found")

        species = Species.get_ensembl_db_prefix(species) if species else None
        self.mode = "w-" if mode == "w" else mode
        if in_memory:
            h5_kwargs = dict(
                driver="core",
                backing_store=False,
            )
        else:
            h5_kwargs = {}

        try:
            self._file = h5py.File(source, mode=self.mode, **h5_kwargs)
        except OSError:
            print(source)
            raise
        self._str2arr = str2arr(moltype="dna")
        self._arr2str = arr2str(moltype="dna")
        self._is_open = True
        if "r" not in self.mode and "species" not in self._file.attrs:
            assert species
            self._file.attrs["species"] = species

        if (
            species
            and (file_species := self._file.attrs.get("species", None)) != species
        ):
            raise ValueError(f"{self.source.name!r} {file_species!r} != {species}")
        self.species = self._file.attrs["species"]

    def __hash__(self):
        return id(self)

    def add_record(self, *, seqid: str, seq: str):
        seq = self._str2arr(seq)
        if seqid in self._file:
            stored = self._file[seqid]
            if (seq == stored).all():
                # already seen this seq
                return
            # but it's different, which is a problem
            raise ValueError(f"{seqid!r} already present but with different seq")
        self._file.create_dataset(
            name=seqid, data=seq, chunks=True, **_HDF5_BLOSC2_KWARGS
        )

    def add_records(self, *, records: typing.Iterable[list[str, str]]):
        for seqid, seq in records:
            self.add_record(seqid=seqid, seq=seq)

    def get_seq_str(
        self, *, seqid: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> str:
        return self._arr2str(self.get_seq_arr(seqid=seqid, start=start, stop=stop))

    def get_seq_arr(
        self, *, seqid: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> NDArray[numpy.uint8]:
        if not self._is_open:
            raise OSError(f"{self.source.name!r} is closed")

        return self._file[seqid][start:stop]

    def get_coord_names(self) -> tuple[str]:
        """names of chromosomes / contig"""
        return tuple(self._file)


# todo: this wrapping class is required for memory efficiency because
#  the cogent3 SequenceCollection class is not designed for large sequence
#  collections, either large sequences or large numbers of sequences. The
#  longer term solution is improving SequenceCollections,
#  which is underway 🎉
class Genome:
    """class to be replaced by cogent3 sequence collection when that
    has been modernised"""

    def __init__(
        self,
        *,
        species: str,
        seqs: SeqsDataABC,
        annots: EnsemblGffDb,
    ) -> None:
        self.species = species
        self._seqs = seqs
        self.annotation_db = annots

    def get_seq(
        self,
        *,
        seqid: str,
        start: Optional[int] = None,
        stop: Optional[int] = None,
        namer: typing.Callable | None = None,
    ) -> str:
        """returns annotated sequence

        Parameters
        ----------
        seqid
            name of chromosome etc..
        start
            starting position of slice in python coordinates, defaults
            to 0
        stop
            ending position of slice in python coordinates, defaults
            to length of coordinate
        namer
            callback for naming the sequence. Callback must take four
            arguments: species, seqid,start, stop. Default is
            species:seqid:start-stop.
        Notes
        -----
        Annotations partially within region are included.
        """
        seq = self._seqs.get_seq_str(seqid=seqid, start=start, stop=stop)
        if namer:
            name = namer(self.species, seqid, start, stop)
        else:
            name = f"{self.species}:{seqid}:{start}-{stop}"
        # we use seqid to make the sequence here because that identifies the
        # parent seq identity, required for querying annotations
        seq = make_seq(seq, name=seqid, moltype="dna")
        seq.name = name
        if self.annotation_db:
            seq.annotation_offset = start or 0
            seq.annotation_db = self.annotation_db.subset(
                seqid=seqid, start=start, stop=stop, allow_partial=True
            )
        return seq

    def get_features(
        self,
        *,
        biotype: str = None,
        seqid: str = None,
        name: str = None,
        start: int = None,
        stop: int = None,
    ) -> typing.Iterable[Feature]:
        """yields features in blocks of seqid"""
        kwargs = {k: v for k, v in locals().items() if k not in ("self", "seqid") and v}
        if seqid:
            seqids = [seqid]
        else:
            seqids = {
                ft["seqid"] for ft in self.annotation_db.get_features_matching(**kwargs)
            }
        for seqid in seqids:
            try:
                seq = self.get_seq(seqid=seqid)
            except TypeError:
                msg = f"ERROR (report me): {self.species!r}, {seqid!r}"
                raise TypeError(msg)
            # because self.get_seq() automatically names seqs differently
            seq.name = seqid
            yield from seq.get_features(**kwargs)

    def get_ids_for_biotype(self, biotype, limit=None):
        annot_db = self.annotation_db
        sql = "SELECT name from gff WHERE biotype=?"
        if limit:
            sql += " LIMIT ?"
        for result in annot_db._execute_sql(sql, (biotype, limit)):
            yield result["name"].split(":")[-1]

    def close(self):
        self._seqs.close()
        self.annotation_db.db.close()


def load_genome(*, config: InstalledConfig, species: str):
    """returns the Genome with bound seqs and features"""
    genome_path = config.installed_genome(species) / _SEQDB_NAME
    seqs = SeqsDataHdf5(source=genome_path, species=species, mode="r")
    ann_path = config.installed_genome(species) / _ANNOTDB_NAME
    ann = EnsemblGffDb(source=ann_path)
    return Genome(species=species, seqs=seqs, annots=ann)


def get_seqs_for_ids(
    *,
    config: InstalledConfig,
    species: str,
    names: list[str],
    make_seq_name: typing.Callable = None,
) -> typing.Iterable[Sequence]:
    genome = load_genome(config=config, species=species)
    # is it possible to do batch query for all names?
    for name in names:
        feature = list(genome.get_features(name=f"%{name}"))[0]
        transcripts = list(feature.get_children(biotype="mRNA"))
        if not transcripts:
            continue

        longest = max(transcripts, key=lambda x: len(x))
        cds = list(longest.get_children(biotype="CDS"))
        if not cds:
            continue

        feature = cds[0]
        seq = feature.get_slice()
        if callable(make_seq_name):
            seq.name = make_seq_name(feature)
        else:
            seq.name = f"{species}-{name}"
        seq.info["species"] = species
        seq.info["name"] = name
        # disconnect from annotation so the closure of the genome
        # does not cause issues when run in parallel
        seq.annotation_db = None
        yield seq

    genome.close()
    del genome


def get_annotations_for_species(
    *, config: InstalledConfig, species: str
) -> GffAnnotationDb:
    """returns the annotation Db for species"""
    path = config.installed_genome(species=species)
    if not path.exists():
        click.secho(f"{species!r} not in {str(config.install_path.parent)!r}", fg="red")
        exit(1)
    # TODO: this filename should be defined in one place
    path = path / "features.gff3db"
    if not path.exists():
        click.secho(f"{path.name!r} is missing", fg="red")
        exit(1)
    return GffAnnotationDb(source=path)


def get_gene_table_for_species(
    *, annot_db: GffAnnotationDb, limit: Optional[int], species: Optional[str] = None
) -> Table:
    """
    returns gene data from a GffDb

    Parameters
    ----------
    annot_db
        feature db
    limit
        limit number of records to
    species
        species name, overrides inference from annot_db.source
    """
    species = species or annot_db.source.parent.name

    columns = (
        "species",
        "name",
        "seqid",
        "source",
        "biotype",
        "start",
        "stop",
        "score",
        "strand",
        "phase",
    )
    rows = []
    for i, record in enumerate(annot_db.get_records_matching(biotype="gene")):
        rows.append([species] + [record.get(c, None) for c in columns[1:]])
        if i == limit:
            break

    return make_table(header=columns, data=rows)


def get_species_summary(
    *, annot_db: GffAnnotationDb, species: Optional[str] = None
) -> Table:
    """
    returns the Table summarising data for species_name

    Parameters
    ----------
    annot_db
        feature db
    species
        species name, overrides inference from annot_db.source
    """
    from ._species import Species

    # for now, just biotype
    species = species or annot_db.source.parent.name
    counts = annot_db.biotype_counts()
    try:
        common_name = Species.get_common_name(species)
    except ValueError:
        common_name = species

    return Table(
        header=("biotype", "count"),
        data=list(counts.items()),
        title=f"{common_name} features",
    )
