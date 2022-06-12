import pandas as pd

m_t = pd.read_csv("files\manga_tracking.csv")
m_t_e = m_t[m_t["已阅读话数"] > 10]

print((m_t_e["出版社"].value_counts(sort=True)/m_t["出版社"].value_counts(sort=True)).sort_values(ascending=False))