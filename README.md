# Muro de novedades oficiales de Nike (gratis, sin código)

Una página web que se actualiza sola 3 veces al día con las publicaciones
oficiales de Nike (web, about.nike.com, YouTube y, si quieres, Instagram),
con filtros por plataforma y categoría. El correo es opcional.

## Puesta en marcha (unos 10 minutos, solo clics)
1. Crea una cuenta gratis en github.com.
2. Pulsa **New repository**, ponle un nombre (ej. `nike-novedades`), marca **Public**
   y créalo. (Público es necesario para que la página sea gratis; solo contiene
   publicaciones oficiales de Nike.)
3. En el repositorio, pulsa **Add file → Upload files**, arrastra TODO el contenido
   de la carpeta descomprimida (incluida la carpeta oculta `.github`) y pulsa **Commit changes**.
4. Ve a **Settings → Pages**, y en **Source** elige **GitHub Actions**.
5. Ve a la pestaña **Actions**, entra en "Nike news wall" y pulsa **Run workflow**.
6. Cuando termine (1-2 min), tu página estará en
   `https://TU-USUARIO.github.io/NOMBRE-DEL-REPOSITORIO/`. Guárdala en favoritos.

A partir de ahí se actualiza sola.

## Instagram
Instagram no permite leer cuentas gratis de forma fiable. Opciones:
- Dejarlo fuera (la página funciona igual con web y YouTube).
- Usar el plan gratuito de RSS.app (2 feeds): crea el feed de, por ejemplo,
  @nike y pega su URL en `sources.yaml`, cambiando `enabled: false` por `enabled: true`.

## Si una fuente falla
Las webs sin RSS dependen del diseño de la página de Nike. Si algo deja de salir,
vuelve a pedírselo a Claude y se ajusta.

## Correo (opcional)
Si añades los datos de tu correo como *Secrets* del repositorio
(`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`), recibirás
además un resumen con lo nuevo.
