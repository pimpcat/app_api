import pandas as pd
from psycopg import connect

# Conexión interna de Docker (mantén el host que te funcionó)
conn = connect("dbname=atlas user=postgres password=Postgres2026* host=db_mapas port=5432")

# Abrimos un archivo para escribir el resultado de forma limpia
with open("/app/diccionario_c_rnc.txt", "w", encoding="utf-8") as f:
    f.write("==================================================\n")
    f.write("      DICCIONARIO DE DATOS: TABLA ATLAS.C_RNC     \n")
    f.write("==================================================\n\n")

    # 1. Obtener las columnas
    query_columnas = """
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_schema = 'atlas' AND table_name = 'c_rnc'
        ORDER BY ordinal_position;
    """
    df_columnas = pd.read_sql_query(query_columnas, conn)

    # 2. Recorrer y guardar
    for index, row in df_columnas.iterrows():
        col = row['column_name']
        tipo = row['data_type']
        
        f.write(f"🔹 Campo: {col} | Tipo: {tipo}\n")
        print(f"Procesando: {col}...") # Para que veas el avance en consola
        
        if "geometry" in tipo or col == "the_geom":
            f.write("  -> [Contiene datos geométricos espaciales]\n\n")
            continue
            
        try:
            query_unicos = f"SELECT DISTINCT {col} FROM atlas.c_rnc WHERE {col} IS NOT NULL LIMIT 15;"
            df_unicos = pd.read_sql_query(query_unicos, conn)
            valores = df_unicos[col].tolist()
            
            if len(valores) == 0:
                f.write("  -> [Columna completamente vacía (NULL)]\n\n")
            else:
                f.write(f"  -> Valores únicos (Muestra): {valores}\n\n")
                
        except Exception as e:
            f.write(f"  -> No se pudieron extraer valores únicos: {e}\n\n")

conn.close()
f.write("--- Fin del reporte ---")
print("¡Listo! El archivo se guardó como 'diccionario_c_rnc.txt' en tu carpeta app_api")