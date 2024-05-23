from __future__ import annotations

import dataclasses
import typing

import blosc2

from cogent3.app.composable import LOADER, define_app
from cogent3.app.io import compress, decompress, pickle_it, unpickle_it
from cogent3.app.typing import IdentifierType, SerialisableType
from cogent3.parse.table import FilteringParser
from cogent3.util.io import iter_splitlines

from ensembl_lite._config import InstalledConfig
from ensembl_lite._db_base import SqliteDbMixin


_HOMOLOGYDB_NAME = "homologies.sqlitedb"

compressor = compress(compressor=blosc2.compress2)
decompressor = decompress(decompressor=blosc2.decompress2)
pickler = pickle_it()
unpickler = unpickle_it()
inflate = decompressor + unpickler


@dataclasses.dataclass(slots=True)
class species_genes:
    """contains gene IDs for species"""

    species: str
    gene_ids: list[str] = None

    def __hash__(self):
        return hash(self.species)

    def __eq__(self, other):
        return self.species == other.species and self.gene_ids == other.gene_ids

    def __post_init__(self):
        if not self.gene_ids:
            self.gene_ids = []
        else:
            self.gene_ids = list(self.gene_ids)

    def __getstate__(self) -> tuple[str, tuple[str, ...]]:
        return self.species, tuple(self.gene_ids)

    def __setstate__(self, args):
        species, gene_ids = args
        self.species = species
        self.gene_ids = list(gene_ids)


@dataclasses.dataclass
class homolog_group:
    """has species_genes instances belonging to the same ortholog group"""

    relationship: str
    gene_ids: typing.Optional[set[str, ...]] = None

    def __post_init__(self):
        self.gene_ids = self.gene_ids if self.gene_ids else set()

    def __hash__(self):
        # allow hashing, but bearing in mind we are updating
        # gene values
        return hash((hash(self.relationship), id(self.gene_ids)))

    def __eq__(self, other):
        return (
            self.relationship == other.relationship and self.gene_ids == other.gene_ids
        )

    def __getstate__(self) -> tuple[str, set[str]]:
        return self.relationship, self.gene_ids

    def __setstate__(self, state):
        relationship, gene_ids = state
        self.relationship = relationship
        self.gene_ids = gene_ids

    def __len__(self):
        return len(self.gene_ids)

    def __or__(self, other):
        if other.relationship != self.relationship:
            raise ValueError(
                f"relationship type {self.relationship!r} != {other.relationship!r}"
            )
        return self.__class__(
            relationship=self.relationship, gene_ids=self.gene_ids | other.gene_ids
        )


T = dict[str, tuple[homolog_group, ...]]


def grouped_related(
    data: typing.Iterable[tuple[str, str, str]],
) -> T:
    """determines related groups of genes

    Parameters
    ----------
    data
        list of full records from the HomologyDb

    Returns
    -------
    a data structure that can be json serialised

    Notes
    -----
    I assume that for a specific relationship type, a gene can only belong
    to one group.
    """
    # grouped is {<relationship type>: {gene id: homolog_group}. So gene's
    # that belong to the same group have the same value
    grouped = {}
    for rel_type, gene_id_1, gene_id_2 in data:
        relationship = grouped.get(rel_type, {})
        pair = {gene_id_1, gene_id_2}

        if gene_id_1 in relationship:
            val = relationship[gene_id_1]
        elif gene_id_2 in relationship:
            val = relationship[gene_id_2]
        else:
            val = homolog_group(relationship=rel_type)
        val.gene_ids |= pair

        relationship[gene_id_1] = relationship[gene_id_2] = val
        grouped[rel_type] = relationship

    reduced = {}
    for rel_type, groups in grouped.items():
        reduced[rel_type] = tuple(set(groups.values()))

    return reduced


def _gene_id_to_group(series: tuple[homolog_group, ...]) -> dict[str:homolog_group]:
    """converts series of homolog_group instances to {geneid: groupl, ..}"""
    result = {}
    for group in series:
        result.update({gene_id: group for gene_id in group.gene_ids})
    return result


def _add_unique(
    a: dict[str, homolog_group],
    b: dict[str, homolog_group],
    combined: dict[str, homolog_group],
) -> dict[str, homolog_group]:
    unique = a.keys() - b.keys()
    combined.update(**{gene_id: a[gene_id] for gene_id in unique})
    return combined


def merge_grouped(group1: T, group2: T) -> T:
    """merges homolog_group with overlapping members"""
    joint = {}
    groups = group1, group2
    rel_types = group1.keys() | group2.keys()
    for rel_type in rel_types:
        if any(rel_type not in grp for grp in groups):
            joint[rel_type] = group1.get(rel_type, group2.get(rel_type))
            continue

        # expand values to dicts
        grp1 = _gene_id_to_group(group1[rel_type])
        grp2 = _gene_id_to_group(group2[rel_type])

        # if a group is unique for a relationship type, not one member
        # will be present in the other group
        # add groups that are truly unique to each
        rel_type_group = {}
        # unique to grp 1
        rel_type_group = _add_unique(grp1, grp2, rel_type_group)
        # unique to grp 2
        rel_type_group = _add_unique(grp2, grp1, rel_type_group)

        shared_ids = grp1.keys() & grp2.keys()
        skip = set()  # id's for groups already processed
        for gene_id in shared_ids:
            if gene_id in skip:
                continue
            merged = grp1[gene_id] | grp2[gene_id]
            rel_type_group.update({gene_id: merged for gene_id in merged.gene_ids})
            skip.update(merged.gene_ids)

        joint[rel_type] = tuple(set(rel_type_group.values()))

    return joint


# the homology db stores pairwise relationship information
class HomologyDb(SqliteDbMixin):
    table_names = "homology", "relationship", "member"

    _relationship_schema = {
        "homology_type": "TEXT",
        "id": "INTEGER PRIMARY KEY",
    }
    _homology_schema = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "relationship_id": "INTEGER",
    }
    _member_schema = {
        "gene_id": "TEXT",  # stableid of gene, defined by Ensembl
        "homology_id": "INTEGER",
        "PRIMARY KEY": ("gene_id", "homology_id"),
    }

    _index_columns = {
        "homology": ("relationship_id",),
        "relationship": ("homology_type",),
        "member": ("gene_id", "homology_id"),
    }

    def __init__(self, source=":memory:"):
        self.source = source
        self._relationship_types = {}
        self._init_tables()
        self._create_views()

    def _create_views(self):
        """define views to simplify queries"""
        # we want to be able to query for all ortholog groups of a
        # particular type. For example, get all groups of IDs of
        # type one-to-one orthologs
        sql = """
        CREATE VIEW IF NOT EXISTS related_groups AS
        SELECT r.homology_type as homology_type,
                r.id as relationship_id,
                h.id as homology_id , m.gene_id as gene_id
        FROM homology h JOIN relationship r ON h.relationship_id = r.id
        JOIN member as m ON m.homology_id = h.id
        """
        self._execute_sql(sql)

    def _make_relationship_type_id(self, rel_type: str) -> int:
        """returns the relationship.id value for relationship_type"""
        if rel_type not in self._relationship_types:
            sql = "INSERT INTO relationship(homology_type) VALUES (?) RETURNING id"
            result = self.db.execute(sql, (rel_type,)).fetchone()[0]
            self._relationship_types[rel_type] = result
        return self._relationship_types[rel_type]

    def _get_homology_group_id(
        self, *, relationship_id: int, gene_ids: typing.Optional[tuple[str]] = None
    ) -> int:
        """creates a new homolog table entry for this relationship id"""
        if gene_ids is None:
            sql = "INSERT INTO homology(relationship_id) VALUES (?) RETURNING id"
            return self.db.execute(sql, (relationship_id,)).fetchone()[0]

        # check if gene_ids exist
        id_placeholders = ",".join("?" * len(gene_ids))
        sql = f"""
        SELECT r.homology_id as homology_id
        FROM related_groups r
        WHERE r.relationship_id = ? AND r.gene_id IN ({id_placeholders}) 
        LIMIT 1
        """
        result = self.db.execute(sql, (relationship_id,) + gene_ids).fetchone()
        if result is None:
            return self._get_homology_group_id(relationship_id=relationship_id)

        return result[0]

    def add_records(
        self,
        *,
        records: typing.Sequence[homolog_group],
        relationship_type: str,
    ) -> None:
        """inserts homology data from records

        Parameters
        ----------
        records
            a sequence of homolog group instances, all with the same
            relationship type
        relationship_type
            the relationship type
        """
        assert relationship_type is not None
        rel_type_id = self._make_relationship_type_id(relationship_type)
        # we now iterate over the homology groups
        # we get a new homology id, then add all genes for that group
        # using the IGNORE to skip duplicates
        sql = "INSERT OR IGNORE INTO member(gene_id,homology_id) VALUES (?, ?)"
        for group in records:
            if group.relationship != relationship_type:
                raise ValueError(f"{group.relationship=} != {relationship_type=}")

            homology_id = self._get_homology_group_id(
                relationship_id=rel_type_id, gene_ids=tuple(group.gene_ids)
            )
            values = [(gene_id, homology_id) for gene_id in group.gene_ids]
            self.db.executemany(sql, values)
        self.db.commit()

    def get_related_to(
        self, *, gene_id: str, relationship_type: str
    ) -> tuple[str, ...]:
        """return genes with relationship type to gene_id"""
        sql = """
        SELECT r.homology_id as homology_id
        FROM related_groups r
        WHERE r.homology_type = ? AND r.gene_id = ?
        """
        homology_id = self._execute_sql(sql, (relationship_type, gene_id)).fetchone()
        if not homology_id:
            return ()
        homology_id = homology_id["homology_id"]
        sql = """
        SELECT GROUP_CONCAT(r.gene_id) as gene_ids
        FROM related_groups r
        WHERE r.homology_id = ?
        """
        result = self._execute_sql(sql, (homology_id,)).fetchone()
        return result["gene_ids"].split(",")

    def get_related_groups(
        self, relationship_type: str
    ) -> typing.Sequence[homolog_group]:
        """returns all groups of relationship type"""
        sql = """
        SELECT GROUP_CONCAT(r.gene_id) as gene_ids
        FROM related_groups r
        WHERE r.homology_type = ?
        GROUP BY r.homology_id
        """
        return [
            homolog_group(
                relationship=relationship_type,
                gene_ids=set(group["gene_ids"].split(",")),
            )
            for group in self._execute_sql(sql, (relationship_type,)).fetchall()
        ]

    def make_indexes(self):
        """adds db indexes for core attributes"""
        sql = "CREATE INDEX IF NOT EXISTS %s on %s(%s)"
        for table_name, cols in self._index_columns.items():
            for col in cols:
                self._execute_sql(sql % (col, table_name, col))

    def num_records(self):
        return list(
            self._execute_sql("SELECT COUNT(*) as count FROM member").fetchone()
        )[0]


def load_homology_db(
    *,
    config: InstalledConfig,
) -> HomologyDb:
    return HomologyDb(source=config.homologies_path / _HOMOLOGYDB_NAME)


@define_app(app_type=LOADER)
class load_homologies:
    def __init__(self, allowed_species: set):
        self._allowed_species = allowed_species
        # map the Ensembl columns to HomologyDb columns

        self.src_cols = (
            "homology_type",
            "species",
            "gene_stable_id",
            "homology_species",
            "homology_gene_stable_id",
        )
        self.dest_col = (
            "relationship",
            "species_1",
            "gene_id_1",
            "species_2",
            "gene_id_2",
        )
        self._reader = FilteringParser(
            row_condition=self._matching_species, columns=self.src_cols, sep="\t"
        )

    def _matching_species(self, row):
        return {row[1], row[3]} <= self._allowed_species

    def main(self, path: IdentifierType) -> SerialisableType:
        parser = self._reader(iter_splitlines(path, chunk_size=500_000))
        header = next(parser)
        assert list(header) == list(self.src_cols), (header, self.src_cols)
        return grouped_related((row[0], row[2], row[4]) for row in parser)
