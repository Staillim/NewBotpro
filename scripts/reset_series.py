"""
reset_series.py — Borra series/anime de la DB para re-indexarlas desde cero.

Uso:
    cd bot
    python scripts/reset_series.py --db "postgresql+asyncpg://user:pass@host/db"
    python scripts/reset_series.py --db "..." --all      # borra TODAS
    python scripts/reset_series.py --db "..." --id 3 7   # borra por ID

Si $DATABASE_URL está en el entorno, --db es opcional.
"""

import asyncio
import sys
import os

# Asegura que el módulo raíz del bot esté en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Resolver DATABASE_URL ANTES de importar settings ──────────────────────────
_args = sys.argv[1:]
if "--db" in _args:
    _db_idx = _args.index("--db")
    _db_url = _args[_db_idx + 1]
    os.environ["DATABASE_URL"] = _db_url
    # Quitar --db y su valor para que el resto de argparse no se confunda
    sys.argv = [sys.argv[0]] + [a for i, a in enumerate(_args) if i != _db_idx and i != _db_idx + 1]
elif not os.environ.get("DATABASE_URL"):
    print(
        "ERROR: Debes pasar la URL de la base de datos de producción:\n"
        '  python scripts/reset_series.py --db "postgresql+asyncpg://user:pass@host/db"\n'
        "O exportar DATABASE_URL antes de ejecutar el script."
    )
    sys.exit(1)

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from database.models import TvShow, Episode, ContentType

_engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
_session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def list_shows() -> list[dict]:
    async with _session() as s:
        result = await s.execute(
            select(
                TvShow.id,
                TvShow.name,
                TvShow.content_type,
                TvShow.published,
                func.count(Episode.id).label("ep_count"),
            )
            .outerjoin(Episode, Episode.tv_show_id == TvShow.id)
            .group_by(TvShow.id)
            .order_by(TvShow.id)
        )
        return [
            {
                "id": row.id,
                "name": row.name,
                "type": row.content_type.value,
                "published": row.published,
                "episodes": row.ep_count,
            }
            for row in result.all()
        ]


async def delete_shows(ids: list[int]) -> dict:
    async with _session() as s:
        ep_result = await s.execute(
            delete(Episode).where(Episode.tv_show_id.in_(ids))
        )
        sh_result = await s.execute(
            delete(TvShow).where(TvShow.id.in_(ids))
        )
        await s.commit()
        return {
            "shows_deleted": sh_result.rowcount,
            "episodes_deleted": ep_result.rowcount,
        }


async def main():
    shows = await list_shows()

    if not shows:
        print("No hay series/anime en la base de datos.")
        await _engine.dispose()
        return

    # ── Mostrar tabla ──────────────────────────────────────────────────────
    print()
    print(f"{'ID':>4}  {'Tipo':<8}  {'Pub':^3}  {'Eps':>4}  Nombre")
    print("─" * 70)
    for s in shows:
        pub = "✓" if s["published"] else "·"
        print(f"{s['id']:>4}  {s['type']:<8}  {pub:^3}  {s['episodes']:>4}  {s['name']}")
    print("─" * 70)
    print(f"Total: {len(shows)} título(s)\n")

    # ── Determinar qué borrar ──────────────────────────────────────────────
    args = sys.argv[1:]

    if "--all" in args:
        ids_to_delete = [s["id"] for s in shows]
        print(f"⚠️  Borrando TODAS ({len(ids_to_delete)}) series/anime…\n")

    elif "--id" in args:
        idx = args.index("--id")
        try:
            ids_to_delete = [int(x) for x in args[idx + 1:]]
        except ValueError:
            print("Error: --id debe ir seguido de números. Ej: --id 3 7")
            await _engine.dispose()
            return
        # Filtrar solo los que existen
        valid = {s["id"] for s in shows}
        ids_to_delete = [i for i in ids_to_delete if i in valid]
        if not ids_to_delete:
            print("Ningún ID válido encontrado.")
            await _engine.dispose()
            return

    else:
        # Modo interactivo
        raw = input(
            "Ingresa los IDs a borrar separados por comas (o 'all' para todos): "
        ).strip()
        if raw.lower() == "all":
            ids_to_delete = [s["id"] for s in shows]
        else:
            try:
                ids_to_delete = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                print("Entrada inválida.")
                await _engine.dispose()
                return

    if not ids_to_delete:
        print("Nada que borrar.")
        await _engine.dispose()
        return

    # Confirmar
    names = [s["name"] for s in shows if s["id"] in ids_to_delete]
    print("Se borrarán:")
    for n in names:
        print(f"  • {n}")
    confirm = input("\n¿Confirmar? (s/N): ").strip().lower()
    if confirm not in ("s", "si", "sí", "y", "yes"):
        print("Cancelado.")
        await _engine.dispose()
        return

    # ── Borrar ──────────────────────────────────────────────────────────────
    result = await delete_shows(ids_to_delete)
    print(
        f"\n✅ Listo: {result['shows_deleted']} serie(s) y "
        f"{result['episodes_deleted']} episodio(s) eliminados.\n"
    )
    print(
        "Ahora puedes re-enviar los episodios al canal de intake:\n"
        "  1. Escribe:  serie: <Nombre>\n"
        "  2. Reenvía todos los episodios en orden\n"
        "  3. Escribe:  final\n"
    )

    await _engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
