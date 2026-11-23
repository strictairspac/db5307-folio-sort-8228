export function route() {
  const [key, id, extra] = (location.hash.slice(1) || 'cases').split('/');
  return {key, id:Number(id) || 0, extra:Number(extra) || 0};
}
export function go(to) {
  location.hash = to;
}
